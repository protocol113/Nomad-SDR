#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Receive-only Nomad control SDK. Semtech command layouts: SWDR001 v3.0.0.

Persistent connections preserve radio configuration. A board-wide lock keeps
radio selection and each USB transaction atomic when two jobs share one UART.
"""
from collections import deque
import csv
import math
from pathlib import Path
import struct
import threading
import time
import uuid

from bridge import Bridge, BridgeError
from frequency import valid_frequency, receiver_band
from boards import BoardError, rf_switch_command
from nomad_rx import NomadReceiver

LORA_BW = {125000:4,250000:5,500000:6,203125:13,406250:14,812500:15}
GFSK_BW = {4800:31,5800:23,7300:15,9700:30,11700:22,14600:14,19500:29,
           23400:21,29300:13,39000:28,46900:20,58600:12,78200:27,93800:19,
           117300:11,156200:26,187200:18,234300:10,312000:25,373600:17,467000:9}
RX_OPCODES={'011c','0110','0112','010e','010f','0114','020e','0111','020b',
            '0229','020f','0210','022b','0206','0224','0225','0113','0227',
            '0209','020d','0218'}


class BusyError(RuntimeError):
    pass


def integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{name} must be an integer in {low}..{high}')
    return value


def fields(data, allowed):
    if not isinstance(data, dict) or set(data)-set(allowed):
        raise ValueError('Unknown fields or non-object JSON')


def boolean(value, name):
    if type(value) is not bool:
        raise ValueError(f'{name} must be boolean')
    return value


def validate_profile(data):
    fields(data, ('modem','frequency_hz','bandwidth_hz','sf','coding_rate','preamble',
                  'sync_word','crc','implicit','iq_inverted','payload_length',
                  'bit_rate','deviation_hz','whitening','crc_seed','crc_polynomial','whitening_seed'))
    p=dict(data)
    modem=p.setdefault('modem','lora')
    if modem not in ('lora','gfsk'):raise ValueError('modem must be lora or gfsk')
    f=p.setdefault('frequency_hz',915000000)
    if not valid_frequency(f):raise ValueError('Frequency must be within 150..2500 MHz')
    integer(f,150000000,2500000000,'frequency_hz')
    boolean(p.setdefault('crc',True),'crc')
    integer(p.setdefault('payload_length',255),1,255,'payload_length')
    integer(p.setdefault('preamble',8 if modem=='lora' else 32),4,65535,'preamble')
    if modem=='lora':
        if any(k in p for k in ('bit_rate','deviation_hz','whitening','crc_seed','crc_polynomial','whitening_seed')):
            raise ValueError('GFSK parameters require modem gfsk')
        bw=p.setdefault('bandwidth_hz',203125 if f>=2300000000 else 125000)
        if type(bw) is not int or bw not in LORA_BW or ((bw in (203125,406250,812500)) != (f>=2300000000)):
            raise ValueError('LoRa BW: sub-GHz 125000/250000/500000; high band 203125/406250/812500')
        integer(p.setdefault('sf',7),5,12,'sf')
        integer(p.setdefault('coding_rate',5),5,8,'coding_rate denominator')
        integer(p.setdefault('sync_word',0x12),0,255,'sync_word')
        boolean(p.setdefault('implicit',False),'implicit')
        boolean(p.setdefault('iq_inverted',False),'iq_inverted')
    else:
        if any(k in p for k in ('sf','coding_rate','implicit','iq_inverted')):
            raise ValueError('LoRa parameters require modem lora')
        integer(p.setdefault('bit_rate',50000),2400,300000,'bit_rate')
        integer(p.setdefault('deviation_hz',25000),1,200000,'deviation_hz')
        bw=p.setdefault('bandwidth_hz',156200)
        if type(bw) is not int or bw not in GFSK_BW:raise ValueError('Unsupported GFSK bandwidth')
        if bw < p['bit_rate']+2*p['deviation_hz']:raise ValueError('GFSK bandwidth must cover bitrate + 2*deviation')
        sync=p.setdefault('sync_word','12345678')
        if (not isinstance(sync,str) or len(sync) not in range(2,17,2)
                or any(c not in '0123456789abcdefABCDEF' for c in sync)):
            raise ValueError('GFSK sync_word must be 1..8 hex bytes')
        try:bytes.fromhex(sync)
        except ValueError:raise ValueError('Invalid GFSK sync_word hex') from None
        boolean(p.setdefault('whitening',True),'whitening')
        integer(p.setdefault('crc_seed',0x1d0f),0,0xffffffff,'crc_seed')
        integer(p.setdefault('crc_polynomial',0x1021),1,0xffffffff,'crc_polynomial')
        integer(p.setdefault('whitening_seed',0x1ff),0,0x1ff,'whitening_seed')
    return p


def sweep_options(data):
    fields(data,('start_hz','stop_hz','step_hz','dwell_ms','samples','cycles','capacity','profile'))
    start=integer(data.get('start_hz',902000000),150000000,2500000000,'start_hz')
    stop=integer(data.get('stop_hz',928000000),start,2500000000,'stop_hz')
    step=integer(data.get('step_hz',1000000),1,100000000,'step_hz')
    if (stop-start)//step+1>4096:raise ValueError('Sweep is limited to 4096 channels')
    profile=data.get('profile',{})
    if not isinstance(profile,dict):raise ValueError('profile must be an object')
    profile=validate_profile(dict(profile,frequency_hz=start))
    if profile['modem']=='lora' and (start<2300000000<=stop):raise ValueError('Split a LoRa sweep at the high-band boundary')
    return dict(start_hz=start,stop_hz=stop,step_hz=step,
                dwell_ms=integer(data.get('dwell_ms',10),1,1000,'dwell_ms'),
                samples=integer(data.get('samples',4),1,100,'samples'),
                cycles=integer(data.get('cycles',1),0,1000000,'cycles'),
                capacity=integer(data.get('capacity',256),1,4096,'capacity'),profile=profile)


def packet_options(data,default_profile=None):
    fields(data,('profile','poll_ms','duration_s','capacity'))
    duration=data.get('duration_s',0)
    if type(duration) not in (int,float) or not math.isfinite(duration) or not 0<=duration<=86400:
        raise ValueError('duration_s must be finite in 0..86400; zero is indefinite')
    return dict(profile=validate_profile(data.get('profile',default_profile or {})),
                poll_ms=integer(data.get('poll_ms',10),1,1000,'poll_ms'),duration_s=duration,
                capacity=integer(data.get('capacity',256),1,4096,'capacity'))


def packet_record(modem, payload, status, irq, crc_present=True):
    result=dict(payload_hex=payload.hex(),length=len(payload),crc_ok=not bool(irq&128),
                timestamp_ns=time.time_ns(),irq=irq)
    if modem=='lora':
        if len(status)!=3:raise ValueError('LoRa packet status must have three bytes')
        snr=struct.unpack('b',status[1:2])[0]/4
        result.update(rssi_dbm=-status[0]/2,snr_db=snr,signal_rssi_dbm=-status[2]/2)
    else:
        if len(status)!=4:raise ValueError('GFSK packet status must have four bytes')
        result.update(rssi_dbm=-status[1]/2,sync_rssi_dbm=-status[0]/2,
                      crc_ok=not bool((irq&128) or (status[3]&16)),packet_flags=status[3])
    result['crc_present']=crc_present
    if not crc_present:result['crc_ok']=None
    return result


class Board:
    def __init__(self, serial=None, host=None, factory=Bridge):
        if bool(serial)==bool(host):raise ValueError('Select serial or host')
        self.serial,self.host,self.factory=serial,host,factory
        self.lock=threading.RLock();self.connections={};self.selected=None;self.closed=False

    def call(self, radio, method, *args, **kwargs):
        integer(radio,0,1,'radio')
        with self.lock:
            if self.closed:raise RuntimeError('Board connection is closed')
            key=0 if self.serial else radio
            if key not in self.connections:
                endpoint=self.serial or f'socket://{self.host}:{1121+radio}'
                self.connections[key]=self.factory(endpoint,nomad_radio=radio if self.serial else None,timeout_ms=1500)
                if self.serial:self.selected=radio
            b=self.connections[key]
            try:
                if self.serial and self.selected!=radio:
                    result=b.request(f'radio {radio}')
                    if result.get('radio')!=radio:raise BridgeError('Radio selection mismatch')
                    self.selected=radio
                return getattr(b,method)(*args,**kwargs)
            except Exception:
                if getattr(b,'_desynced',False):
                    b.close();self.connections.pop(key,None);self.selected=None
                raise

    def view(self, radio):
        board=self
        class View:
            def __getattr__(self,name):
                if name=='_payload':return Bridge._payload
                return lambda *a,**kw:board.call(radio,name,*a,**kw)
        return View()

    def close(self):
        with self.lock:
            self.closed=True
            for b in self.connections.values():b.close()
            self.connections.clear();self.selected=None


class Job:
    def __init__(self, kind, capacity=256):
        self.id=uuid.uuid4().hex;self.kind=kind;self.cancel=threading.Event()
        self.lock=threading.RLock();self.rows=deque(maxlen=capacity)
        self.count=0;self.evicted=0;self.error=None;self.state='running'
        self.payload_bytes=0
        self.started_ns=time.time_ns();self.finished_ns=None;self.thread=None
        self.counters=dict(crc_errors=0,header_errors=0,hardware_drops=None)

    def add(self,row):
        with self.lock:
            if len(self.rows)==self.rows.maxlen:self.evicted+=1
            self.rows.append(dict(index=self.count,**row));self.count+=1
            self.payload_bytes+=row.get('length',0)

    def snapshot(self):
        with self.lock:
            elapsed=((self.finished_ns or time.time_ns())-self.started_ns)/1000000000
            return dict(id=self.id,kind=self.kind,state=self.state,count=self.count,
                        buffer_evictions=self.evicted,error=self.error,started_ns=self.started_ns,
                        finished_ns=self.finished_ns,stop_requested=self.cancel.is_set(),
                        counters=dict(self.counters),results=list(self.rows),elapsed_s=elapsed,
                        results_per_second=self.count/elapsed if elapsed>0 else 0,
                        payload_bytes=self.payload_bytes,payload_bytes_per_second=self.payload_bytes/elapsed if elapsed>0 else 0)


class Radio:
    def __init__(self, board, index):
        self.board,self.index=board,index;self.b=board.view(index)
        self.rx=NomadReceiver(self.b);self.lock=threading.RLock()
        self.profile=None;self.running=False;self.job=None;self.jobs={}
        self.gain=dict(agc=True,step=0,rx_boosted=False,lna='differential')
        self.tuned=None
        self.identity=None
        self.agc_default=None

    def idle(self):
        if self.job and self.job.thread and self.job.thread.is_alive():raise BusyError('Radio has an active job; stop it first')

    def read(self, command, length):
        r=self.b.read_command(bytes.fromhex(command),length)
        return self.b._payload(r,length)

    def write(self, command, mode=None):
        if command[:4].lower() not in RX_OPCODES:raise ValueError('Opcode is not part of the RX control API')
        self.rx.write(command,mode)

    def configure(self, data):
        p=validate_profile(data)
        with self.lock,self.board.lock:
            self.idle()
            self.profile=None;self.running=False
            try:rf_switch=rf_switch_command(self.b.info())
            except BoardError as error:raise ValueError(str(error)) from None
            if self.b.version()['data']!='22030103':
                raise ValueError('Expected LR1121 transceiver 0x0103')
            self.identity=dict(radio=self.index,version='22030103',uid=self.read('0125',8).hex(),
                               packed_extension_state='per-session-verification-required')
            self.b.speed(500000);self.b.reset()
            self.agc_default=self.rx.memory(0xf20230)[0]
            for cmd,mode in (('011c00',1),('011001',1),(rf_switch,1),
                             ('010e',1),('010f3f',1),('011400800000',1),('011c01',2)):
                self.write(cmd,mode)
            self.write('020e'+('02' if p['modem']=='lora' else '01'),2)
            self.tune(p['frequency_hz'])
            if p['modem']=='lora':
                ldro=int(2**p['sf']/p['bandwidth_hz']>=.016)
                self.write('020f'+bytes((p['sf'],LORA_BW[p['bandwidth_hz']],p['coding_rate']-4,ldro)).hex(),2)
                packet=p['preamble'].to_bytes(2,'big')+bytes((int(p['implicit']),p['payload_length'],int(p['crc']),int(p['iq_inverted'])))
                self.write('0210'+packet.hex(),2);self.write(f'022b{p["sync_word"]:02x}',2)
            else:
                modulation=struct.pack('>IBBI',p['bit_rate'],9,GFSK_BW[p['bandwidth_hz']],p['deviation_hz'])
                self.write('020f'+modulation.hex(),2)
                sync=bytes.fromhex(p['sync_word'])
                packet=p['preamble'].to_bytes(2,'big')+bytes((4,len(sync)*8,0,1,p['payload_length'],2 if p['crc'] else 1,int(p['whitening'])))
                self.write('0210'+packet.hex(),2);self.write('0206'+sync.ljust(8,b'\0').hex(),2)
                self.write('0224'+struct.pack('>II',p['crc_seed'],p['crc_polynomial']).hex(),2)
                self.write('0225'+p['whitening_seed'].to_bytes(2,'big').hex(),2)
            # Receive/CAD events only. No auto-TX or CAD-TX exit mode.
            self.write('0113000007f800000000',2)
            self.profile=p
            return self.status()

    def tune(self, frequency):
        self.write('011c01',2);self.running=False
        if 400000000<=frequency<=1000000000:
            self.write(f'0111{(frequency//1000000-4)//4:02x}{(frequency//1000000+7)//4:02x}',1)
            self.write('011c01',2)
        self.write('020b'+frequency.to_bytes(4,'big').hex(),2)
        calibration='764643cececc8c980907ee' if frequency>=2300000000 else '2232434564556676060000' if 850000000<=frequency<=930000000 else 'cc0e314463666666060000' if 410000000<=frequency<=510000000 else None
        if calibration:self.write('0229'+calibration,2)
        self.tuned=frequency

    def _apply_gain(self):
        g=self.gain
        self.write('0227'+('01' if g['rx_boosted'] else '00'))
        self.rx.masked(0xf20214,0x00080000,0 if g['agc'] else 0x00080000)
        if self.agc_default is None:raise RuntimeError('No saved AGC baseline; configure RX again')
        control=(self.agc_default&0x71110000) if g['agc'] else 0x71100000
        self.rx.masked(0xf20230,0x71110000,control)
        if not g['agc']:self.rx.masked(0xf20214,0x00f00000,g['step']<<20)
        self.rx.masked(0xf3008c,0xf0,{'single_n':1,'single_p':2,'differential':3}[g['lna']]<<4)
        value=self.rx.memory(0xf20214)[0];lna=self.rx.memory(0xf3008c)[0]
        agc=self.rx.memory(0xf20230)[0]
        if (bool(value&0x80000)==g['agc'] or (not g['agc'] and (value>>20)&15!=g['step'])
                or (lna>>4)&15!={'single_n':1,'single_p':2,'differential':3}[g['lna']]
                or (agc&0x71110000)!=control):
            raise RuntimeError('Gain/LNA register readback mismatch')
        return dict(**g,gain_register=value,lna_register=lna,agc_control_register=agc,sensitivity_verified=False)

    def set_gain(self,data):
        fields(data,('agc','step','rx_boosted','lna'))
        g=dict(self.gain,**data)
        boolean(g['agc'],'agc');boolean(g['rx_boosted'],'rx_boosted');integer(g['step'],0,15,'step')
        if g['lna'] not in ('single_n','single_p','differential'):raise ValueError('Unknown LF0 LNA topology')
        with self.lock,self.board.lock:
            self.idle()
            if not self.profile:raise ValueError('Configure RX first')
            previous=self.gain;self.gain=g
            try:return self._apply_gain()
            except Exception:self.gain=previous;raise

    def _start(self):
        if not self.profile:raise ValueError('Configure RX first')
        self.write('0114ffffffff');self.write('0209ffffff',4)
        self.running=True;self._apply_gain()
        return self.status()

    def start(self):
        with self.lock,self.board.lock:self.idle();return self._start()

    def _stop(self):
        self.write('011c00',1);self.running=False

    def stop(self):
        result=self.stop_job()
        with self.lock,self.board.lock:
            if self.job and self.job.thread.is_alive():return result
            self._stop();return self.status()

    def status(self):
        with self.lock:
            return dict(radio=self.index,profile=dict(self.profile) if self.profile else None,
                        running=self.running,frequency_hz=self.tuned,gain=dict(self.gain),
                        job=self.job.snapshot() if self.job else None)

    def diagnostics(self):
        with self.lock,self.board.lock:
            self.idle();resume=self.running
            try:
                self.write('011c01',2)
                vbat=self.read('0119',1)[0];raw=int.from_bytes(self.read('011a',2),'big')&2047
                return dict(radio=self.index,version=self.b.version()['data'],vbat_raw=vbat,temp_raw=raw,
                            supply_v=round((5*vbat/255-1)*1.35,3),
                            die_temp_c=round(((raw/2047*1.35)-.7295)*1000/-1.7+25,2),
                            calibrated=False,errors=self.b.errors())
            finally:
                if resume:self._start()
                else:self._stop()

    def cad(self,data):
        fields(data,('symbols','detect_peak','detect_min','timeout_ms'))
        symbols=data.get('symbols',2)
        if type(symbols) is not int or symbols not in (1,2,4,8,16):raise ValueError('CAD symbols must be 1/2/4/8/16')
        peak=integer(data.get('detect_peak',22),0,255,'detect_peak')
        minimum=integer(data.get('detect_min',10),0,255,'detect_min')
        timeout=integer(data.get('timeout_ms',1000),10,10000,'timeout_ms')
        with self.lock:
            self.idle()
            if not self.profile or self.profile['modem']!='lora':raise ValueError('CAD requires configured LoRa RX')
            resume=self.running
            try:
                self.write('011c01',2);self.running=False;self.write('0114ffffffff',2)
                self.write('020d'+bytes((symbols,peak,minimum,0,0,0,0)).hex(),2)
                self.write('0218')
                deadline=time.monotonic()+timeout/1000
                while True:
                    irq=int(self.b.status()['decoded']['irq_status'],16)
                    if irq&256:return dict(detected=bool(irq&512),irq=irq,timeout=False)
                    if time.monotonic()>=deadline:return dict(detected=None,irq=irq,timeout=True)
                    time.sleep(.005)
            finally:
                if resume:self._start()
                else:self._stop()

    def launch(self,kind,target,capacity=256):
        with self.lock:
            self.idle();job=Job(kind,capacity)
            def worker():
                try:target(job)
                except Exception as exc:job.error=str(exc)
                finally:
                    try:
                        with self.lock,self.board.lock:self._stop()
                    except Exception as exc:job.error=job.error or str(exc)
                    with job.lock:
                        job.state='error' if job.error else 'cancelled' if job.cancel.is_set() else 'complete'
                        job.finished_ns=time.time_ns()
            job.thread=threading.Thread(target=worker,name=f'nomad-{kind}-{self.index}',daemon=True)
            self.job=job;self.jobs[job.id]=job
            # Keep bounded history, preserving the currently active job.
            while len(self.jobs)>16:self.jobs.pop(next(iter(self.jobs)))
            job.thread.start();return job.snapshot()

    def stop_job(self,expected_kind=None):
        job=self.job
        if job and expected_kind and job.kind!=expected_kind:
            if job.thread and job.thread.is_alive():raise BusyError('A different job owns this radio')
            return dict(state='idle')
        if job and job.thread and job.thread.is_alive():
            job.cancel.set();job.thread.join(5)
        return job.snapshot() if job else dict(state='idle')

    def sweep(self,data):
        opts=sweep_options(data)
        start,stop,step=(opts[k] for k in ('start_hz','stop_hz','step_hz'))
        dwell,samples,cycles,capacity,profile=(opts[k] for k in ('dwell_ms','samples','cycles','capacity','profile'))
        with self.lock:
            self.idle();self.configure(profile)
            def scan(job):
                cycle=0
                while not job.cancel.is_set() and (cycles==0 or cycle<cycles):
                    for frequency in range(start,stop+1,step):
                        if job.cancel.is_set():break
                        with self.lock,self.board.lock:self.tune(frequency);self._start()
                        if job.cancel.wait(dwell/1000):break
                        values=[]
                        for _ in range(samples):
                            if job.cancel.is_set():break
                            with self.lock:self_value=-self.read('0205',1)[0]/2
                            values.append(self_value)
                        if values:job.add(dict(cycle=cycle,frequency_hz=frequency,min_dbm=min(values),
                                               max_dbm=max(values),mean_dbm=sum(values)/len(values),samples=len(values),timestamp_ns=time.time_ns()))
                    cycle+=1
            return self.launch('sweep',scan,capacity)

    def packet_start(self,data):
        opts=packet_options(data,self.profile)
        poll,duration,capacity,profile=(opts[k] for k in ('poll_ms','duration_s','capacity','profile'))
        with self.lock:
            self.idle();self.configure(profile);self._start()
            def monitor(job):
                deadline=time.monotonic()+duration if duration else float('inf')
                while not job.cancel.is_set() and time.monotonic()<deadline:
                    with self.lock,self.board.lock:
                        irq=int(self.b.status()['decoded']['irq_status'],16)
                        if irq&128:job.counters['crc_errors']+=1
                        if irq&64:job.counters['header_errors']+=1
                        if irq&8:
                            # Freeze the packet buffer while copying it. The
                            # resulting receive gap is not a measured drop count.
                            self.write('011c01',2);self.running=False
                            length,offset=self.read('0203',2)
                            payload=self.read('010a'+bytes((offset,length)).hex(),length) if length else b''
                            status=self.read('0204',3 if profile['modem']=='lora' else 4)
                            crc_present=bool(self.read('0230',1)[0]&16) if profile['modem']=='lora' else profile['crc']
                            job.add(packet_record(profile['modem'],payload,status,irq,crc_present))
                        if irq:self.write('0114'+irq.to_bytes(4,'big').hex())
                        if irq&8:self._start()
                    job.cancel.wait(poll/1000)
            return self.launch('packet',monitor,capacity)


class Control:
    def __init__(self,board):
        self.board=board;self.radios=[Radio(board,i) for i in range(2)]
        self.lock=threading.RLock();self.dual_plan=None

    def capabilities(self):
        native=self.board.view(0).request('capabilities')
        identities=[]
        for r in self.radios:
            with r.lock:
                if not r.identity:
                    r.identity=dict(radio=r.index,version=r.b.version()['data'],uid=r.read('0125',8).hex(),
                                    packed_extension_state='per-session-verification-required')
                identities.append(dict(r.identity))
        return dict(native=native,api_schema=1,transport='serial' if self.board.serial else 'tcp',
                    endpoints=['rx','sweep','packet','cad','gain','diagnostics','stream/status','dual'],
                    modems=['lora','gfsk'],lora_bandwidths_hz=list(LORA_BW),gfsk_bandwidths_hz=list(GFSK_BW),
                    jobs_share_serial=True,dual_modes=['sweep','packet'],transmit=False,radio_capabilities=identities,
                    gfsk_bit_rate_range=[2400,300000],lora_sf_range=[5,12])

    def dual_configure(self,data):
        fields(data,('radios',))
        plans=data.get('radios')
        if not isinstance(plans,list) or len(plans)!=2:raise ValueError('radios must contain exactly two plans')
        checked=[]
        for plan in plans:
            fields(plan,('mode','settings'))
            if plan.get('mode') not in ('sweep','packet') or not isinstance(plan.get('settings',{}),dict):raise ValueError('Dual mode must be sweep or packet')
            settings=sweep_options(plan.get('settings',{})) if plan['mode']=='sweep' else packet_options(plan.get('settings',{}))
            checked.append(dict(mode=plan['mode'],settings=settings))
        with self.lock:
            for r in self.radios:
                with r.lock:r.idle()
            self.dual_plan=checked
            return dict(configured=True,radios=checked,started=False)

    def dual_start(self):
        with self.lock:
            if self.dual_plan is None:raise ValueError('Configure dual plan first')
            for r in self.radios:
                with r.lock:r.idle()
            started=[]
            try:
                for r,plan in zip(self.radios,self.dual_plan):
                    started.append(r.sweep(plan.get('settings',{})) if plan['mode']=='sweep' else r.packet_start(plan.get('settings',{})))
                return dict(radios=started)
            except Exception:
                for r in self.radios:
                    try:r.stop() if r.profile else r.stop_job()
                    except Exception:pass
                raise

    def close(self):
        for r in self.radios:r.stop_job()
        for r in self.radios:
            try:
                if r.profile:r.stop()
            except Exception:pass
        self.board.close()


def export_job(job,path):
    rows=job.snapshot()['results']
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    names=sorted({key for row in rows for key in row}) or ['index']
    with path.open('w',newline='',encoding='utf-8') as f:
        writer=csv.DictWriter(f,fieldnames=names);writer.writeheader();writer.writerows(rows)
    return path
