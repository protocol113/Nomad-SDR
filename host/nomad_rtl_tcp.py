#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Nomad LIQ8 -> rtl_tcp bridge for SDR++ and SDRangel (receive only)."""
from __future__ import annotations
import argparse
from dataclasses import dataclass
import json
import logging
import math
import queue
from pathlib import Path
import socket
import struct
import threading
import time

import numpy as np
from scipy.signal import firwin, lfilter
from bridge import Bridge, BridgeError
from nomad_rx import NomadReceiver, read_frame
from frequency import valid_frequency, receiver_band

NATIVE_RATE = 62500
OUTPUT_RATES = (62500, 125000, 250000)
DONGLE_HEADER = struct.pack('>4sII', b'RTL0', 0, 0)
COMMAND = struct.Struct('>BI')
LOG = logging.getLogger('nomad-rtl-tcp')


class Interpolator:
    """Stateful integer FIR interpolation: no independent block-edge resampling."""
    def __init__(self, output_rate,input_rate=NATIVE_RATE):
        if output_rate not in OUTPUT_RATES:
            raise ValueError('Select 62500, 125000, or 250000 S/s (250 kHz in SDR++)')
        if output_rate%input_rate:
            raise ValueError('Output rate must be an integer multiple of the stream rate')
        self.factor = output_rate // input_rate
        if self.factor > 1:
            self.taps = firwin(32*self.factor+1, .90/self.factor)*self.factor
            self.state = np.zeros(len(self.taps)-1, dtype=np.complex128)

    def process(self, samples):
        if self.factor == 1:
            return samples
        expanded = np.zeros(len(samples)*self.factor, dtype=np.complex64)
        expanded[::self.factor] = samples
        result, self.state = lfilter(self.taps, [1.0], expanded, zi=self.state)
        return result


def unsigned_iq(samples, gain_db):
    values = np.empty(len(samples)*2, dtype=np.float64)
    values[::2] = samples.real
    values[1::2] = samples.imag
    # rtl_tcp uses offset-binary interleaved I,Q with midpoint 127.5.
    return np.clip(np.rint(values*(127.5*10.0**(gain_db/20.0))+127.5), 0, 255).astype(np.uint8).tobytes()


@dataclass(frozen=True)
class Settings:
    frequency: int
    output_rate: int
    ppm: int = 0

    @property
    def corrected_frequency(self):
        return round(self.frequency*(1+self.ppm/1_000_000))


class ClientSession:
    def __init__(self, client, host, radio, frequency, output_rate, gain_db, report=None,stream_rate=31250,buffer_ms=250,serial=None,packed_verification=None,narrow=False):
        self.client, self.host, self.radio = client, host, radio
        self.serial = serial
        self.packed_verification = packed_verification
        self.gain_db, self.report_path = gain_db, report
        self.stream_rate,self.buffer_ms=stream_rate,buffer_ms
        # Narrow: the LR1121 itself delivers 31.25 kS/s (BW 0x0E, ~11 kHz usable),
        # one 125-sample bank per 4 ms. Half the SPI load: needed for two radios.
        self.narrow=narrow
        self.bank_ms=4 if narrow else 2
        self.settings = Settings(frequency, output_rate)
        self.lock = threading.Lock()
        self.generation = 0
        self.stop = threading.Event()
        self.last_change = time.monotonic()
        self.stats = dict(host=host, radio=radio, native_rate=NATIVE_RATE, gain_db=gain_db,
                          stream_rate=stream_rate,buffer_ms=buffer_ms,narrow=narrow,underruns=0,
                          clients=1, connected=True, jobs=[], received_frames=0, forwarded_samples=0, error=None)
        self.ignored = set()

    def record(self):
        if self.report_path:
            path = Path(self.report_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(path.suffix+'.tmp')
            temp.write_text(json.dumps(self.stats, indent=2)+'\n', encoding='utf-8')
            temp.replace(path)

    def snapshot(self):
        with self.lock:
            return self.settings, self.generation

    def apply_command(self, command, value):
        with self.lock:
            current = self.settings
            if command == 1:
                if not valid_frequency(value):
                    raise ValueError(f'{value} Hz is outside the LR1121 synthesizer range: 150..2500 MHz')
                updated = Settings(value, current.output_rate, current.ppm)
            elif command == 2:
                if value not in OUTPUT_RATES:
                    raise ValueError(f'Unsupported output rate {value}; select 250 kHz in SDR++ or 62500/125000/250000 in SDRangel')
                updated = Settings(current.frequency, value, current.ppm)
            elif command == 5:
                ppm = value if value < 0x80000000 else value-0x100000000
                if abs(ppm) > 200:
                    raise ValueError('PPM correction must be within -200..200')
                updated = Settings(current.frequency, current.output_rate, ppm)
            else:
                if command not in self.ignored:
                    LOG.info('Ignoring RTL tuner control 0x%02x: LR1121 uses chip AGC; software gain is %.1f dB', command, self.gain_db)
                    self.ignored.add(command)
                return
            if not valid_frequency(updated.corrected_frequency):
                    raise ValueError('PPM-adjusted frequency is outside the LR1121 synthesizer range')
            if updated != current:
                self.settings = updated
                self.generation += 1
                self.last_change = time.monotonic()

    def read_commands(self):
        pending = bytearray()
        try:
            while not self.stop.is_set():
                try:
                    data = self.client.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    break
                pending.extend(data)
                while len(pending) >= COMMAND.size:
                    command, value = COMMAND.unpack_from(pending)
                    del pending[:COMMAND.size]
                    self.apply_command(command, value)
        except (ConnectionResetError, BrokenPipeError):
            self.stats['disconnect_reason'] = 'client_disconnect'
        except (OSError, ValueError) as exc:
            if not self.stop.is_set():
                self.stats['error'] = str(exc)
                LOG.error('%s', exc)
        finally:
            self.stop.set()

    def settle_settings(self):
        # Collapse initial GUI settings and rapid dial movements into one setup.
        while not self.stop.is_set():
            with self.lock:
                quiet = time.monotonic()-self.last_change
            if quiet >= .15:
                return
            self.stop.wait(.02)

    def pipe_stream(self,b,job,generation,interpolator):
        """Read the radio independently of GUI pacing, with bounded jitter storage."""
        frames=queue.Queue(maxsize=500) # One second of native 125-sample banks.
        finished=threading.Event();abort=threading.Event();errors=[]
        cancelled=[False]
        def receive():
            sequence=0;last=time.monotonic()
            try:
                while True:
                    _,latest=self.snapshot()
                    if (self.stop.is_set() or abort.is_set() or latest!=generation) and not cancelled[0]:
                        if b.io.write(b'\x03')!=1:raise BridgeError('Failed to cancel acquisition')
                        b.io.flush();cancelled[0]=True
                    data,terminal=read_frame(b.io,b._id,self.stream_rate,sequence,compact=True)
                    if terminal is not None:
                        job['terminal']=terminal
                        if terminal['flags']&~(4 if cancelled[0] else 0) or terminal['lost_samples']:
                            raise BridgeError(f'Acquisition stopped with loss: {terminal}')
                        if not cancelled[0]:raise BridgeError('Unexpected end of live acquisition')
                        return
                    current=time.monotonic()
                    job['max_input_gap_ms']=max(job.get('max_input_gap_ms',0),(current-last)*1000)
                    last=current;sequence+=1;job['frames']=sequence
                    self.stats['received_frames']+=1
                    if not cancelled[0]:
                        try:frames.put_nowait(data)
                        except queue.Full:raise BridgeError('GUI output stalled: host jitter buffer full')
            except Exception as exc:errors.append(exc)
            finally:finished.set()
        reader=threading.Thread(target=receive,name='nomad-iq',daemon=True);reader.start()
        prefill=max(1,round(self.buffer_ms/self.bank_ms)) # 125-sample bank: 2 ms, or 4 ms narrow.
        started=False;epoch=0;sent_samples=0;last_report=time.monotonic()
        try:
            while not finished.is_set() or not frames.empty():
                _,latest=self.snapshot()
                if self.stop.is_set() or latest!=generation:abort.set();break
                if not started:
                    if frames.qsize()<prefill and not finished.is_set():
                        self.stop.wait(.005);continue
                    epoch=time.monotonic();sent_samples=0;started=True
                try:data=frames.get(timeout=.05)
                except queue.Empty:
                    if finished.is_set():break
                    self.stats['underruns']+=1;started=False
                    LOG.warning('Radio input paused; refilling the host jitter buffer')
                    continue
                batch=[data]
                for _ in range(7):
                    try:batch.append(frames.get_nowait())
                    except queue.Empty:break
                samples=np.frombuffer(b''.join(batch),dtype='<c8')
                output=interpolator.process(samples)
                delay=epoch+sent_samples/job['output_rate']-time.monotonic()
                if delay>0 and self.stop.wait(delay):break
                self.client.sendall(unsigned_iq(output,self.gain_db))
                sent_samples+=len(output);self.stats['forwarded_samples']+=len(output)
                self.stats['buffered_ms']=frames.qsize()*self.bank_ms
                if time.monotonic()-last_report>=1:self.record();last_report=time.monotonic()
        finally:
            abort.set()
            reader.join(timeout=6)
            if reader.is_alive():raise BridgeError('Radio reader did not stop in time')
        if errors:raise errors[0]

    def run(self):
        self.client.settimeout(.25)
        self.client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.client.sendall(DONGLE_HEADER)
        commands = threading.Thread(target=self.read_commands, name='rtl-commands', daemon=True)
        commands.start()
        try:
            if self.narrow and (self.packed_verification is not None or self.stream_rate!=31250):
                raise BridgeError('Narrow mode is native 31250 S/s; it does not combine with packed or 62500')
            if self.serial and self.packed_verification is not None and self.stream_rate!=31250:
                raise BridgeError('Experimental packed USB live requires --stream-rate 31250 to fit the UART')
            while not self.stop.is_set():
                self.settle_settings()
                settings, generation = self.snapshot()
                if self.stop.is_set():
                    break
                interpolator = Interpolator(settings.output_rate,self.stream_rate)
                LOG.info('Radio %d: %.6f MHz, native %.1f kS/s -> output %.1f kS/s',
                         self.radio, settings.frequency/1e6, self.stream_rate/1000, settings.output_rate/1000)
                if receiver_band(settings.frequency)=='experimental_synth_range':
                    LOG.warning('Experimental synthesizer frequency: receiver sensitivity is not specified in this band')
                job = dict(frequency_hz=settings.frequency, pll_frequency_hz=settings.corrected_frequency,
                           output_rate=settings.output_rate, receiver_band=receiver_band(settings.frequency),frames=0,terminal=None)
                self.stats['jobs'].append(job)
                endpoint = self.serial or f'socket://{self.host}:{1121+self.radio}'
                with Bridge(endpoint, timeout_ms=1500, nomad_radio=self.radio if self.serial else None) as b:
                    info = b.info()
                    if info.get('compact_stream') != 'LIQ8':
                        raise BridgeError('Install Nomad firmware 0.4.0 or newer for compact live streaming')
                    if self.narrow and 31250 not in (b.request('capabilities').get('native_rates') or []):
                        raise BridgeError('Install firmware with native 31250 S/s (narrow) support')
                    if not self.narrow and self.stream_rate==31250 and info.get('decimated_stream')!='LID8':
                        raise BridgeError('Install Nomad firmware 0.5.0 for the stable 31.25 kS/s stream')
                    verified=None
                    if self.packed_verification is not None:
                        from nomad_extension_probe import prepare_packed
                        verified=json.loads(Path(self.packed_verification).read_text())
                        prepare_packed(b,settings.corrected_frequency,NATIVE_RATE,verified)
                        job['acquisition']='arc8'
                    else:
                        NomadReceiver(b).configure(settings.corrected_frequency, 31250 if self.narrow else NATIVE_RATE)
                    if self.stop.is_set():
                        break
                    command='streampd' if self.stream_rate==31250 else 'streamp8'
                    if self.narrow:wire='stream8 31250 0'
                    else:wire=f'{command} {NATIVE_RATE} 0 {verified["uid"]}' if verified is not None else f'{"streamd" if self.stream_rate==31250 else "stream8"} {NATIVE_RATE} 0'
                    ack = b.request(wire)
                    if verified is not None and ack.get('acquisition')!='arc8':
                        raise BridgeError('Expected ARC acquisition acknowledgement',ack)
                    if ack.get('stream') != ('LID8' if self.stream_rate==31250 and not self.narrow else 'LIQ8') or ack.get('format') != 'iq8_block_float':
                        raise BridgeError('Unexpected compact stream acknowledgement', ack)
                    self.pipe_stream(b,job,generation,interpolator)
                self.record()
        except (ConnectionResetError, BrokenPipeError):
            self.stats['disconnect_reason'] = 'client_disconnect'
        except (OSError, ValueError, RuntimeError) as exc:
            if not self.stop.is_set():
                self.stats['error'] = str(exc)
                LOG.error('Client stopped: %s', exc)
        finally:
            self.stop.set()
            try:
                self.client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            commands.join(timeout=1)
            self.client.close()
            self.stats['connected'] = False
            self.record()


def serve(host='192.168.4.1', radio=0, listen='127.0.0.1', port=1234,
          frequency=915000000, output_rate=250000, gain_db=60.0, report=None, ready=None, stop=None,stream_rate=31250,buffer_ms=250,serial=None,packed_verification=None,narrow=False):
    stop = stop or threading.Event()
    with socket.socket() as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((listen, port))
        server.listen(1)
        server.settimeout(.25)
        if ready:
            ready(server.getsockname()[1])
        target = serial or host
        LOG.info('Ready: rtl_tcp %s:%d -> Nomad %s radio %d', listen, server.getsockname()[1], target, radio)
        LOG.info('Select 250 kHz and tune within 150..2500 MHz; RF performance outside Nomad matching bands is unverified')
        try:
            while not stop.is_set():
                try:
                    client, address = server.accept()
                except socket.timeout:
                    continue
                LOG.info('Client connected: %s', address)
                ClientSession(client, host, radio, frequency, output_rate, gain_db, report,stream_rate,buffer_ms,serial,packed_verification,narrow).run()
        except KeyboardInterrupt:
            LOG.info('Server stopped')


def radio_report(report, radio):
    """build/x.json -> build/x.radio0.json so two feeds never share a status file."""
    return None if report is None else Path(report).with_suffix(f'.radio{radio}{Path(report).suffix}')


def serve_both(host='192.168.4.1', listen='127.0.0.1', ports=(1234, 1235),
               frequencies=(915000000, 2440000000), packed_verifications=(None, None),
               report=None, ready=None, stop=None, **options):
    """Two independent rtl_tcp feeds, radio 0 and radio 1, over Wi-Fi.

    Each radio has its own firmware TCP session (1121 / 1122), so the feeds
    tune, start and stop independently. They are not sample-synchronized.
    With a per-chip packed verification each feed is full-width (62.5 kS/s
    acquired, ~20 kHz usable); otherwise each is narrow (31.25 kS/s native).
    """
    stop = stop or threading.Event()
    threads = [threading.Thread(target=serve, name=f'radio{radio}', daemon=True, kwargs=dict(
        options, host=host, radio=radio, listen=listen, port=ports[radio], frequency=frequencies[radio],
        packed_verification=packed_verifications[radio],
        report=radio_report(report, radio), stop=stop,
        ready=(lambda port, radio=radio: ready(radio, port)) if ready else None)) for radio in (0, 1)]
    for thread in threads:
        thread.start()
    try:
        while any(thread.is_alive() for thread in threads):
            if stop.wait(.25):
                break
            if not all(thread.is_alive() for thread in threads):
                LOG.error('One radio feed stopped; stopping both')
                break
    except KeyboardInterrupt:
        LOG.info('Server stopped')
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=8)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host', default=None, help='Nomad IP from the UART wifi command')
    p.add_argument('--serial', help='USB native mode: main ESP32 UART such as COM3')
    p.add_argument('--packed-verification',type=Path,nargs='+',metavar='VERIFY_JSON',
                   help='ARC packed acquisition: per-chip verify.json. One path for a single radio, '
                        'or two (radio0 radio1) for --radio both full-width dual.')
    p.add_argument('--radio', choices=('0', '1', 'both'), default='0',
                   help="'both' serves radio 0 on --port and radio 1 on --port+1 (Wi-Fi only)")
    p.add_argument('--listen', default='127.0.0.1')
    p.add_argument('--port', type=int, default=1234)
    p.add_argument('--frequency', type=int, default=915000000)
    p.add_argument('--frequency2', type=int, default=2440000000, help='Radio 1 start frequency with --radio both')
    p.add_argument('--narrow', action='store_true',
                   help='Native 31.25 kS/s from the LR1121 (~11 kHz usable, half the SPI load); implied by --radio both')
    p.add_argument('--output-rate', type=int, choices=OUTPUT_RATES, default=250000)
    p.add_argument('--gain-db', type=float, default=60.0, help='Software gain before rtl_tcp 8-bit output')
    p.add_argument('--stream-rate', type=int, choices=(31250,62500), default=None, help='Stream I/Q rate; USB and Wi-Fi default to 31250')
    p.add_argument('--buffer-ms', type=int, choices=range(50,751), metavar='50..750', default=250, help='Host jitter prebuffer in milliseconds')
    p.add_argument('--report', type=Path, default=Path('build/rtl-tcp-status.json'))
    a = p.parse_args(argv)
    both = a.radio == 'both'
    if not valid_frequency(a.frequency) or not math.isfinite(a.gain_db) or not 0 <= a.gain_db <= 100:
        p.error('Choose a supported frequency and software gain 0..100 dB')
    if not 1 <= a.port <= (65534 if both else 65535):
        p.error('Port must be 1..65535 (both radios also use --port+1)')
    if a.serial and a.host:
        p.error('Use either --serial (USB native) or --host (Wi-Fi)')
    if both:
        if a.serial:
            p.error('--radio both needs Wi-Fi (--host): the 921600-baud serial link carries one radio at 31.25 kS/s')
        if not valid_frequency(a.frequency2):
            p.error('--frequency2 is outside the LR1121 synthesizer range: 150..2500 MHz')
        if a.packed_verification is not None:
            # On-chip packing cuts SPI 4x, so two full-width (62.5 kS/s) streams fit.
            if len(a.packed_verification) != 2:
                p.error('--radio both with --packed-verification needs two verify.json paths: radio0 radio1')
            a.narrow = False
        else:
            # Two unpacked full-width streams exceed the shared 16 MHz SPI bus.
            a.narrow = True
    else:
        a.radio = int(a.radio)
        if a.packed_verification is not None and len(a.packed_verification) != 1:
            p.error('a single radio takes one --packed-verification verify.json')
        if a.packed_verification is not None:
            a.packed_verification = a.packed_verification[0]
    if not a.serial and a.host is None:
        a.host = '192.168.4.1'
    if a.narrow and a.stream_rate == 62500:
        p.error('--narrow is the native 31250 S/s stream; drop --stream-rate 62500')
    if a.narrow and a.packed_verification is not None:
        p.error('--narrow does not combine with --packed-verification')
    if a.stream_rate is None:
        a.stream_rate = 31250
    return a


def main():
    a = parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(threadName)s %(message)s')
    options = vars(a)
    if options.pop('radio') == 'both':
        port, frequency, frequency2 = options.pop('port'), options.pop('frequency'), options.pop('frequency2')
        options.pop('serial')
        pv = options.pop('packed_verification')
        packed_verifications = tuple(pv) if pv else (None, None)
        mode = 'full-width packed' if pv else 'narrow'
        LOG.info('Monitoring both radios (%s): radio 0 -> 127.0.0.1:%d, radio 1 -> 127.0.0.1:%d', mode, port, port+1)
        serve_both(ports=(port, port+1), frequencies=(frequency, frequency2),
                   packed_verifications=packed_verifications, **options)
    else:
        options.pop('frequency2')
        serve(radio=a.radio, **options)


if __name__ == '__main__':
    main()
