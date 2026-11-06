/* SPDX-License-Identifier: MIT */
/* ESP32 port of pico/firmware/{main,stream}.c. See docs/nomad.md. */
#include <Arduino.h>
#include <SPI.h>
#include <WiFi.h>
#include <Preferences.h>
#include <atomic>
#include <algorithm>
#include "esp32-hal-spi.h"
#include "soc/spi_struct.h"
#include "soc/gpio_struct.h"
#include "rom/ets_sys.h"
#include "lwip/opt.h"
#include "lwip/api.h"
#include "lwip/tcp.h"
#include "lwip/tcpip.h"
extern "C" {
#include "lwip/priv/sockets_priv.h"
}
#include "board.h"
#include "compact.h"
#include "indicators.h"
#if __has_include("wifi.local.h")
#include "wifi.local.h"
#endif
#ifdef NOMAD_OTA_PASSWORD
#include <ArduinoOTA.h>
#endif
#include <Update.h>
extern "C" {
#include "protocol.h"
#include "stream_format.h"
#include "ook.h"
}

#ifndef NOMAD_AP_SSID
#define NOMAD_AP_SSID "Nomad-SDR"
#endif
#ifndef NOMAD_AP_PASSWORD
#define NOMAD_AP_PASSWORD "nomadsdr1121"
#endif
#ifndef NOMAD_STA_SSID
#define NOMAD_STA_SSID ""
#endif
#ifndef NOMAD_STA_PASSWORD
#define NOMAD_STA_PASSWORD ""
#endif

static bool fallbackAP=false;
static uint64_t networkStarted;
static char staSsid[33];
static char staPass[65];
static bool wifiStored=false;
static Preferences wifiPrefs;
static bool wifiConfigure(const char *line,Print &out,uint32_t id,bool uart);
static bool telemetryRequest(const char *line,Print &out,uint32_t id);

static bool staEnabled() { return staSsid[0]!=0; }
static String jsonString(const String &value) {
    String out="\"";
    for (size_t i=0;i<value.length();++i) {
        uint8_t c=value[i];
        if (c=='\"' || c=='\\') { out+='\\'; out+=char(c); }
        else if (c<32) { char escaped[7]; snprintf(escaped,sizeof(escaped),"\\u%04x",c); out+=escaped; }
        else out+=char(c);
    }
    return out+'\"';
}
static void networkStatus(Print &out,uint32_t id) {
    bool connected=staEnabled() && WiFi.status()==WL_CONNECTED;
    out.printf("{\"id\":%lu,\"ok\":true,\"mode\":\"%s\",\"ssid\":%s,\"connected\":%s,\"status\":%d,\"ip\":\"%s\",\"gateway\":\"%s\",\"subnet\":\"%s\",\"rssi_dbm\":%d,\"ap_enabled\":%s,\"ap_ip\":\"%s\",\"ports\":[1121,1122],\"stored\":%s}\n",
        (unsigned long)id,staEnabled()?(fallbackAP?"AP_STA":"STA"):"AP",
        jsonString(staEnabled()?String(staSsid):String(NOMAD_AP_SSID)).c_str(),
        connected?"true":"false",int(WiFi.status()),WiFi.localIP().toString().c_str(),
        WiFi.gatewayIP().toString().c_str(),WiFi.subnetMask().toString().c_str(),
        connected?int(WiFi.RSSI()):0,fallbackAP?"true":"false",WiFi.softAPIP().toString().c_str(),
        wifiStored?"true":"false");
}
static bool networkRequest(const char *line,Print &out,bool uart) {
    unsigned long id=0; char command[16],extra;
    int tokens=sscanf(line,"%lu %15s %c",&id,command,&extra);
    if (tokens<2 || !id) return false;
    if (indicatorsCommand(line,out,id)) return true;
    if (telemetryRequest(line,out,id)) return true;
    if (!strcmp(command,"wifi")) {
        if (tokens==2) { networkStatus(out,id); return true; }
        return wifiConfigure(line,out,id,uart);
    }
    if (tokens!=2) return false;
    if (!strcmp(command,"netbench")) {
        static const uint8_t zeros[4*TCP_MSS]={0};
        out.printf("{\"id\":%lu,\"ok\":true,\"bytes\":%u}\n",id,unsigned(sizeof(zeros)*64));
        uint64_t start=esp_timer_get_time(); size_t sent=0;
        for (unsigned i=0;i<64;++i) {
            size_t n=out.write(zeros,sizeof(zeros)); sent+=n;
            if (n!=sizeof(zeros)) break;
        }
        out.printf("{\"id\":%lu,\"ok\":true,\"sent\":%u,\"elapsed_us\":%llu,\"free_heap\":%u}\n",id,unsigned(sent),
            (unsigned long long)(esp_timer_get_time()-start),ESP.getFreeHeap());
        return true;
    }
    return false;
}
static void loadWifiCredentials() {
    wifiPrefs.begin("nomadw", true);
    wifiStored=wifiPrefs.isKey("set");
    if (wifiStored) {
        strlcpy(staSsid,wifiPrefs.getString("ssid").c_str(),sizeof(staSsid));
        strlcpy(staPass,wifiPrefs.getString("pass").c_str(),sizeof(staPass));
    } else {
        strlcpy(staSsid,NOMAD_STA_SSID,sizeof(staSsid));
        strlcpy(staPass,NOMAD_STA_PASSWORD,sizeof(staPass));
    }
    wifiPrefs.end();
}
static void saveWifiCredentials() {
    wifiPrefs.begin("nomadw", false);
    wifiPrefs.putUChar("set",1);
    wifiPrefs.putString("ssid",staSsid);
    wifiPrefs.putString("pass",staPass);
    wifiPrefs.end();
    wifiStored=true;
}
static void startNetwork() {
    WiFi.persistent(false);
    WiFi.setHostname(board::hostname);
    WiFi.setSleep(false);
    if (staEnabled()) {
        WiFi.mode(WIFI_STA);
        WiFi.setAutoReconnect(true);
        WiFi.begin(staSsid,staPass);
        fallbackAP=false;
    } else {
        WiFi.mode(WIFI_AP);
        if (!WiFi.softAP(NOMAD_AP_SSID,NOMAD_AP_PASSWORD,1,false,2)) abort();
        fallbackAP=true;
    }
    networkStarted=esp_timer_get_time();
}
static void networkTick() {
    // Leave USB control usable while associating. Open the recovery AP if
    // association fails; the station keeps reconnecting automatically.
    if (staEnabled() && !fallbackAP && WiFi.status()!=WL_CONNECTED &&
        esp_timer_get_time()-networkStarted>=20000000) {
        WiFi.mode(WIFI_AP_STA);
        fallbackAP=WiFi.softAP(NOMAD_AP_SSID,NOMAD_AP_PASSWORD,1,false,2);
    }
}

static SPIClass radioSPI(VSPI);
static SemaphoreHandle_t spiMutex;
static SemaphoreHandle_t streamMutex;
static WiFiServer servers[2] = {WiFiServer(board::port[0]), WiFiServer(board::port[1])};

struct Frame {
    uint32_t sequence, timestamp, samples, payloadBytes;
    uint8_t bytes[IQ_FRAME_BYTES];
};
struct Radio {
    unsigned index;
    WiFiClient client;
    Stream *io = &client;
    SemaphoreHandle_t owner;
    bool serialSession = false;
    bool compact = false;
    bool decimated = false;
    bool packed = false;
    nomad_decimator_t decimator;
    QueueHandle_t queue;
    StaticQueue_t queueControl;
    uint8_t *queueStorage;
    unsigned queueCapacity;
    request_t req;
    char line[BRIDGE_LINE_SIZE];
    uint8_t rx[BRIDGE_MAX_BYTES + 1];
    uint8_t networkGroup[4*TCP_MSS+IQ_FRAME_BYTES];
    uint32_t speed = 500000;
    std::atomic<bool> active{false}, done{false}, stop{false};
    uint32_t flags, produced, readOps, maxQueue, maxBlock, blocks, rate;
    uint32_t jobId;
    std::atomic<uint32_t> sent{0};
    uint64_t elapsed=0;
    // Parser options also change for ordinary RPCs. Keep the last accepted
    // stream's format separately so info/read requests do not corrupt telemetry.
    bool sendBufferGrown=false; // Once per TCP connection; reset on accept.
    bool streamCompact=false,streamDecimated=false,streamPacked=false;
    uint32_t streamRate=0;
    uint64_t start, deadline;
};
static Radio radios[2];

static bool nextToken(const char *&p,char *out,size_t cap) {
    while (*p==' ' || *p=='\t') ++p;
    if (!*p) { if (cap) out[0]=0; return false; }
    size_t n=0;
    if (*p=='"') {
        ++p;
        while (*p && *p!='"') {
            char c=*p++;
            if (c=='\\' && *p) c=*p++;
            if (n+1>=cap) return false;
            out[n++]=c;
        }
        if (*p!='"') return false;
        ++p;
        out[n]=0;
        return true;
    }
    while (*p && *p!=' ' && *p!='\t') {
        if (n+1>=cap) return false;
        out[n++]=*p++;
    }
    out[n]=0;
    return true;
}
static bool wifiConfigure(const char *line,Print &out,uint32_t id,bool uart) {
    const char *p=line;
    char skipped[16],mode[8],ssid[33],pass[65],extra[8];
    if (!nextToken(p,skipped,sizeof(skipped)) || !nextToken(p,skipped,sizeof(skipped))) return false;
    if (!nextToken(p,mode,sizeof(mode))) { networkStatus(out,id); return true; }
    if (!uart) {
        out.printf("{\"id\":%lu,\"ok\":false,\"error\":\"wifi_uart_only\",\"phase\":\"admission\"}\n",(unsigned long)id);
        return true;
    }
    if (radios[0].active.load() || radios[1].active.load()) {
        out.printf("{\"id\":%lu,\"ok\":false,\"error\":\"wifi_busy_streaming\",\"phase\":\"admission\"}\n",(unsigned long)id);
        return true;
    }
    if (!strcmp(mode,"ap")) {
        if (nextToken(p,extra,sizeof(extra))) {
            out.printf("{\"id\":%lu,\"ok\":false,\"error\":\"wifi_ap\",\"phase\":\"parse\"}\n",(unsigned long)id);
            return true;
        }
        staSsid[0]=0; staPass[0]=0;
        saveWifiCredentials();
        WiFi.disconnect(true,true);
        startNetwork();
        networkStatus(out,id);
        return true;
    }
    if (strcmp(mode,"sta") || !nextToken(p,ssid,sizeof(ssid)) || !ssid[0] ||
        !nextToken(p,pass,sizeof(pass)) || nextToken(p,extra,sizeof(extra))) {
        out.printf("{\"id\":%lu,\"ok\":false,\"error\":\"wifi_sta_ssid_password\",\"phase\":\"parse\"}\n",(unsigned long)id);
        return true;
    }
    strlcpy(staSsid,ssid,sizeof(staSsid));
    strlcpy(staPass,pass,sizeof(staPass));
    saveWifiCredentials();
    WiFi.disconnect(true,true);
    startNetwork();
    networkStatus(out,id);
    return true;
}

// The lock covers both CS assertions of read commands. A second radio cannot
// change the bus frequency or interleave its command with a pending response.
struct BusLock {
    BusLock() { xSemaphoreTake(spiMutex, portMAX_DELAY); }
    ~BusLock() { xSemaphoreGive(spiMutex); }
};
static uint64_t now() { return esp_timer_get_time(); }
// Acquisition timing breakdown (only the acquire task writes these). Read with
// "<id> perf", clear with "<id> perf reset". Used to size the dual-radio budget.
static TaskHandle_t acquireTask=nullptr;
static struct Perf {
    uint64_t banks,bankUs,maxBankUs,polls,pollMisses,pollMissUs,busyUs,spiUs,transfers,dspUs;
} perf;
static bool inAcquire() { return acquireTask && xTaskGetCurrentTaskHandle()==acquireTask; }
static std::atomic<bool> otaBusy{false};
static bool telemetryRequest(const char *line,Print &out,uint32_t id) {
    char command[16]={0},action[16]={0},extra;
    int n=sscanf(line,"%*u %15s %15s %c",command,action,&extra);
    if(!strcmp(command,"perf") && (n==1 || (n==2 && !strcmp(action,"reset")))) {
        if(n==2) memset(&perf,0,sizeof(perf));
        out.printf("{\"id\":%lu,\"ok\":true,\"free_heap\":%u,\"largest_block\":%u,\"free_dma\":%u,\"min_free_heap\":%u,\"banks\":%llu,\"bank_us\":%llu,\"avg_bank_us\":%llu,\"max_bank_us\":%llu,\"polls\":%llu,\"poll_misses\":%llu,\"poll_miss_us\":%llu,\"busy_wait_us\":%llu,\"spi_us\":%llu,\"transfers\":%llu,\"dsp_us\":%llu}\n",
            (unsigned long)id,ESP.getFreeHeap(),unsigned(heap_caps_get_largest_free_block(MALLOC_CAP_8BIT)),
            unsigned(heap_caps_get_free_size(MALLOC_CAP_DMA)),ESP.getMinFreeHeap(),perf.banks,perf.bankUs,perf.banks?perf.bankUs/perf.banks:0ull,perf.maxBankUs,perf.polls,perf.pollMisses,perf.pollMissUs,perf.busyUs,perf.spiUs,perf.transfers,perf.dspUs);
        return true;
    }
    if(n==1 && !strcmp(command,"capabilities")) {
        out.printf("{\"id\":%lu,\"ok\":true,\"schema\":1,\"board\":\"%s\",\"rf_switch\":\"%s\",\"radios\":2,\"frequency_hz\":[150000000,2500000000],\"native_rates\":[31250,62500,125000],\"streams\":[\"LIQ1\",\"LIQ8\",\"LID8\"],\"packed_requires_per_chip_verification\":true,\"packed_extension_probed\":false,\"usb_live_native_rate\":62500,\"usb_live_output_rate\":31250,\"uart_baud\":%lu,\"packed_usb_burst_max_banks\":128,\"fan\":%s,\"rgb_leds\":%u,\"control_api\":\"host/nomad_api.py\"}\n",(unsigned long)id,board::name,board::rf_switch,(unsigned long)board::uart_baud,board::has_fan?"true":"false",board::led_count);
        return true;
    }
    if(n!=2 || strcmp(command,"stream") || strcmp(action,"status"))return false;
    out.printf("{\"id\":%lu,\"ok\":true,\"uptime_ms\":%lu,\"free_heap\":%u,\"radios\":[",(unsigned long)id,(unsigned long)millis(),ESP.getFreeHeap());
    for(unsigned i=0;i<2;++i) {
        Radio &r=radios[i];bool active=r.active.load();uint32_t sent=r.sent.load();
        uint64_t elapsed=active&&r.start?now()-r.start:r.elapsed;
        uint64_t dataBytes=r.streamDecimated?uint64_t(sent/2)*340+(sent&1?171:0):uint64_t(sent)*(IQ_HEADER_BYTES+(r.streamCompact?COMPACT_PAYLOAD_BYTES:IQ_PAYLOAD_BYTES));
        out.printf("%s{\"radio\":%u,\"active\":%s,\"job_id\":%lu,\"acquisition\":\"%s\",\"native_rate\":%lu,\"output_rate\":%lu,\"produced\":%lu,\"sent\":%lu,\"pending_frames_estimate\":%lu,\"queue_capacity\":%u,\"max_queue\":%lu,\"flags\":%lu,\"read_ops\":%lu,\"elapsed_us\":%llu",i?",":"",i,active?"true":"false",(unsigned long)r.jobId,r.streamPacked?"arc8":"raw23",(unsigned long)r.streamRate,(unsigned long)(r.streamDecimated?r.streamRate/2:r.streamRate),(unsigned long)r.produced,(unsigned long)sent,(unsigned long)(r.produced>=sent?r.produced-sent:0),r.queueCapacity,(unsigned long)r.maxQueue,(unsigned long)r.flags,(unsigned long)r.readOps,(unsigned long long)elapsed);
        out.printf(",\"data_bytes\":%llu,\"average_bytes_per_second\":%lu,\"loss_detected\":%s}",(unsigned long long)dataBytes,(unsigned long)(elapsed?dataBytes*1000000/elapsed:0),(r.flags&(IQ_DMA_STARVED|IQ_QUEUE_FULL|IQ_USB_LOST|IQ_RADIO_ERROR|IQ_FORMAT_ERROR))?"true":"false");
    }
    out.println("]}");return true;
}
static void updateIndicators() {
    indicatorsTick(radios[0].active.load(),radios[1].active.load(),
        radios[0].flags,radios[1].flags,
        radios[0].queue?uxQueueMessagesWaiting(radios[0].queue):0,radios[0].queueCapacity,
        radios[1].queue?uxQueueMessagesWaiting(radios[1].queue):0,radios[1].queueCapacity);
}
static IRAM_ATTR void be32(uint8_t *p, uint32_t v) {
    p[0]=v>>24; p[1]=v>>16; p[2]=v>>8; p[3]=v;
}
// Radio SPI hot path. Measured on XR4: through the Arduino HAL from flash, a
// 7-byte transfer cost ~130 us (15 us begin, 16 us end, cache-miss bound) and
// the total moved by hundreds of us between builds with code layout. These
// helpers live in IRAM and drive VSPI (SPI3) registers directly, so per-bank
// cost no longer depends on flash cache luck. radioSPI.begin() and one
// transaction in setup() configure pins, full duplex and SPI mode 0, MSB first.
static IRAM_ATTR bool pinHigh(int pin) {
    return pin<32 ? (GPIO.in>>pin)&1 : (GPIO.in1.val>>(pin-32))&1;
}
static IRAM_ATTR void pinWrite(int pin,bool high) {
    if (pin<32) { if (high) GPIO.out_w1ts=1u<<pin; else GPIO.out_w1tc=1u<<pin; }
    else if (high) GPIO.out1_w1ts.val=1u<<(pin-32); else GPIO.out1_w1tc.val=1u<<(pin-32);
}
static IRAM_ATTR bool ready(Radio &r, uint32_t ms) {
    uint64_t start=now(),until=start+uint64_t(ms)*1000;
    uint64_t lastYield=start;
    bool ok=true;
    while (ms && pinHigh(board::busy[r.index])) {
        if (now()>=until) { ok=false; break; }
        if (now()-lastYield>=1000) { vTaskDelay(1); lastYield=now(); }
    }
    if (inAcquire()) perf.busyUs+=now()-start;
    return ok;
}
static uint32_t spiClockFor(uint32_t hz) {
    static uint32_t cachedHz=0,cachedDiv=0; // spiFrequencyToClockDiv is slow
    if (hz!=cachedHz) { cachedDiv=spiFrequencyToClockDiv(hz); cachedHz=hz; }
    return cachedDiv;
}
static IRAM_ATTR void transfer(Radio &r, const uint8_t *tx, uint8_t *rx, size_t n) {
    uint64_t start=now();
    spi_dev_t &spi=SPI3;
    spi.clock.val=spiClockFor(r.speed);
    pinWrite(board::cs[r.index],false);
    ets_delay_us(1);
    for (size_t offset=0;offset<n;offset+=64) {
        size_t count=std::min(size_t(64),n-offset),words=(count+3)/4;
        spi.mosi_dlen.usr_mosi_dbitlen=count*8-1;
        spi.miso_dlen.usr_miso_dbitlen=count*8-1;
        for (size_t w=0;w<words;++w) {
            // Dummy bytes while reading a response must be zero (NOP).
            uint32_t value=0;
            if (tx) for (size_t k=0;k<4 && 4*w+k<count;++k) value|=uint32_t(tx[offset+4*w+k])<<(8*k);
            spi.data_buf[w]=value;
        }
        spi.cmd.usr=1;
        while (spi.cmd.usr) {}
        if (rx) for (size_t w=0;w<words;++w) {
            uint32_t value=spi.data_buf[w];
            for (size_t k=0;k<4 && 4*w+k<count;++k) rx[offset+4*w+k]=uint8_t(value>>(8*k));
        }
    }
    pinWrite(board::cs[r.index],true);
    if (inAcquire()) { perf.spiUs+=now()-start; ++perf.transfers; }
    ets_delay_us(5); // Let BUSY assert before the caller polls it.
}
static IRAM_ATTR bool readMem(Radio &r, uint32_t address, uint32_t words) {
    uint8_t c[7]={1,6,0,0,0,0,uint8_t(words)};
    be32(c+2,address);
    BusLock lock;
    if (!ready(r,20)) return false;
    transfer(r,c,nullptr,sizeof(c));
    if (!ready(r,20)) return false;
    transfer(r,nullptr,r.rx,words*4+1);
    ++r.readOps;
    return (r.rx[0]>>1)==3;
}
static IRAM_ATTR bool word(Radio &r,uint32_t address,uint32_t &value) {
    if (!readMem(r,address,1)) return false;
    value=uint32_t(r.rx[1])<<24|uint32_t(r.rx[2])<<16|uint32_t(r.rx[3])<<8|r.rx[4]; return true;
}
static IRAM_ATTR bool masked(Radio &r,uint32_t address,uint32_t mask,uint32_t value) {
    uint8_t c[14]={1,12}, zero[6]={0}, status[6];
    be32(c+2,address); be32(c+6,mask); be32(c+10,value);
    BusLock lock;
    if (!ready(r,20)) return false;
    transfer(r,c,nullptr,sizeof(c));
    if (!ready(r,20)) return false;
    transfer(r,zero,status,sizeof(status));
    return (status[0]>>1)==2;
}
static bool gate(Radio &r,bool enabled) {
    return masked(r,0xf20014,0x30000010,enabled?0x30000010:0x30000000);
}
static void resetRadio(Radio &r) {
    BusLock lock;
    digitalWrite(board::cs[r.index],HIGH);
    digitalWrite(board::reset[r.index],LOW); delay(10);
    digitalWrite(board::reset[r.index],HIGH); delay(10);
}
static bool pointerValid(uint32_t p) {
    return p==0x800a00 || p==0x800a10 ||
        (p>=IQ_DESC_BASE+32 && p<IQ_DESC_BASE+IQ_BANKS*16 && !(p&15));
}
static bool validate(Radio &r) {
    uint32_t v;
    if (!word(r,0xf20124,v) || ((v>>20)&7)!=(r.rate==31250?6u:r.rate==62500?5u:4u)) return false;
    if (!word(r,0xf20014,v) || (v&0x30000010)!=0x30000000) return false;
    if (!word(r,IQ_POINTER,v) || v!=0x800a00) return false;
    if(r.packed) {
        if(r.req.tx_len!=8)return false;
        const uint8_t commands[2][2]={{1,1},{1,0x25}};
        const uint8_t version[4]={0x22,3,1,3};
        for(unsigned k=0;k<2;++k) {
            BusLock lock;
            if(!ready(r,20))return false;
            transfer(r,commands[k],nullptr,2);
            if(!ready(r,20))return false;
            transfer(r,nullptr,r.rx,k?9:5);
            if((r.rx[0]>>1)!=3 || memcmp(r.rx+1,k?r.req.tx:version,k?8:4))return false;
        }
        const uint32_t addresses[]={0x80f000,0x80f008,0x80f00c,0x80f010,0x80f014,0x80f078,0x8004ac};
        const uint32_t expected[]={0x49515033,1,0,0,8,0x46494631,0x94510};
        for(unsigned i=0;i<7;++i)if(!word(r,addresses[i],v)||v!=expected[i])return false;
        for(unsigned i=0;i<16;++i)if(!word(r,0x80f100+i*4,v)||v)return false;
    }
    unsigned banks=r.packed?16:IQ_BANKS;
    for (unsigned i=0;i<banks;++i) {
        if (!readMem(r,iq_descriptor(i),4)) return false;
        if (iq_be32(r.rx+1)!=IQ_CONTROL || iq_be32(r.rx+5)!=0x901004 ||
            iq_be32(r.rx+9)!=(r.packed?IQ_DATA_BASE+i*2048+996:IQ_DATA_BASE+(i+1)*IQ_BANK_BYTES-4) ||
            iq_be32(r.rx+13)!=iq_descriptor((i+1)%banks)) return false;
    }
    return true;
}
static void finishAcquisition(Radio &r) {
    if (!gate(r,false)) {
        r.flags|=IQ_RADIO_ERROR;
        resetRadio(r); // A stuck command must not leave acquisition running.
    }
    if(r.packed && !masked(r,0x80f008,0xffffffffu,0)) {
        r.flags|=IQ_RADIO_ERROR;resetRadio(r);
    }
    r.done.store(true); // Publish counters before the sender reads them.
}
// One producer on core 1 owns both radios while streaming. Round-robin polling
// avoids spinning on an empty bank of one radio while the other needs service.
static void acquire(void *) {
    Frame frame;
    uint64_t lastYield=now();
    for (;;) {
        bool work=false;
        for (Radio &r:radios) {
            if (!r.active.load() || r.done.load()) continue;
            if (!r.start) {
                r.start=now(); r.deadline=r.start+1000000;
                if (!gate(r,true)) r.flags|=IQ_RADIO_ERROR;
            }
            if (r.stop.load()) r.flags|=IQ_ABORT;
            if (now()>=r.deadline) r.flags|=IQ_DEADLINE;
            if (r.flags || (r.blocks && r.produced>=r.blocks)) { finishAcquisition(r); continue; }
            uint32_t bank=r.produced%(r.packed?16:IQ_BANKS), desc=iq_descriptor(bank), control;
            uint64_t pollStart=now();
            if (!word(r,r.packed?0x80f100+bank*4:desc,control)) { r.flags|=IQ_RADIO_ERROR; finishAcquisition(r); continue; }
            ++perf.polls;
            if (r.packed?!(control&0x80000000u):(control&3)!=0) { ++perf.pollMisses; perf.pollMissUs+=now()-pollStart; continue; }
            work=true;
            uint64_t begin=pollStart;
            uint8_t arcPayload[COMPACT_PAYLOAD_BYTES];
            if(r.packed) {
                if(!readMem(r,IQ_DATA_BASE+bank*2048,64))r.flags|=IQ_RADIO_ERROR;
                else if(iq_be32(r.rx+1)!=control || !nomad_arc8(arcPayload,r.rx+1,r.produced))r.flags|=IQ_FORMAT_ERROR;
                else if(r.decimated && !nomad_expand8(frame.bytes+IQ_HEADER_BYTES,arcPayload))r.flags|=IQ_FORMAT_ERROR;
            } else {
            if ((control&~0x001fff03u)!=(IQ_CONTROL&~0x001fff03u)) r.flags|=IQ_RADIO_ERROR;
            for (unsigned w=0;w<IQ_BANK_BYTES/4 && !r.flags;w+=64) {
                unsigned n=IQ_BANK_BYTES/4-w; if (n>64) n=64;
                if (!readMem(r,IQ_DATA_BASE+bank*IQ_BANK_BYTES+w*4,n)) r.flags|=IQ_RADIO_ERROR;
                else if (!iq_pack23(frame.bytes+IQ_HEADER_BYTES+w*3,r.rx+1,n)) r.flags|=IQ_FORMAT_ERROR;
            }
            }
            uint32_t pointer=0;
            if (!r.flags) {
                if (!word(r,IQ_POINTER,pointer) || !pointerValid(pointer) || (r.packed && pointer>=IQ_DESC_BASE+16*16)) r.flags|=IQ_RADIO_ERROR;
                else if (pointer==desc) {
                    if (!word(r,IQ_POINTER,pointer)) r.flags|=IQ_RADIO_ERROR;
                    else if (pointer==desc) r.flags|=IQ_DMA_STARVED;
                }
            }
            if (!r.flags && !masked(r,desc,0xffffffffu,IQ_CONTROL)) r.flags|=IQ_RADIO_ERROR;
            if(r.packed && !r.flags && !masked(r,0x80f100+bank*4,0xffffffffu,0))r.flags|=IQ_RADIO_ERROR;
            if (r.flags) { finishAcquisition(r); continue; }
            frame.sequence=r.produced; frame.timestamp=uint32_t(now()-r.start);
            frame.samples=IQ_SAMPLES;frame.payloadBytes=IQ_PAYLOAD_BYTES;
            uint64_t dspStart=now();
            if(r.compact) {
                uint8_t packed[COMPACT_PAYLOAD_BYTES];
                if(r.decimated) {
                    frame.samples=nomad_decimate8(packed,frame.bytes+IQ_HEADER_BYTES,&r.decimator);
                    if(!frame.samples)r.flags|=IQ_FORMAT_ERROR;
                    frame.payloadBytes=1+frame.samples*2;
                } else {
                    if(r.packed)memcpy(packed,arcPayload,COMPACT_PAYLOAD_BYTES);
                    else if(!nomad_pack8(packed,frame.bytes+IQ_HEADER_BYTES))r.flags|=IQ_FORMAT_ERROR;
                    frame.payloadBytes=COMPACT_PAYLOAD_BYTES;
                }
                if(r.flags) {finishAcquisition(r);continue;}
                memcpy(frame.bytes+IQ_HEADER_BYTES,packed,frame.payloadBytes);
            }
            perf.dspUs+=now()-dspStart;
            if (xQueueSend(r.queue,&frame,0)!=pdTRUE) { r.flags|=IQ_QUEUE_FULL; finishAcquisition(r); continue; }
            ++r.produced; r.deadline=now()+1000000;
            r.maxQueue=std::max(r.maxQueue,uint32_t(uxQueueMessagesWaiting(r.queue)));
            r.maxBlock=std::max(r.maxBlock,uint32_t(now()-begin));
            ++perf.banks; perf.bankUs+=now()-begin; perf.maxBankUs=std::max(perf.maxBankUs,now()-begin);
        }
        // Keep the idle task/watchdog runnable even during indefinite captures.
        // A 1 ms tick fits the 32-bank (32/64 ms) acquisition window.
        if (!work || now()-lastYield>=10000) { vTaskDelay(1); lastYield=now(); }
    }
}
// The Arduino lwIP build fixes TCP_SND_BUF at 5760 B (4 segments). On a weak
// XR4 link a lost segment then rarely draws 3 duplicate ACKs, so recovery waits
// for the retransmission timer (>= ~0.5 s) and a dual stream's send queue fills
// (measured: 60 s dual runs ended at 33-46 s at -73 dBm). snd_buf counts free
// send space; raising it once on an idle connection lets ~12 segments fly.
// Done inside the tcpip thread; TCP_SND_QUEUELEN (17 pbufs) still caps it.
struct GrowSend { int fd; SemaphoreHandle_t done; bool ok; };
static void growSendInTcpip(void *arg) {
    GrowSend &g=*static_cast<GrowSend *>(arg);
    struct lwip_sock *sock=lwip_socket_dbg_get_socket(g.fd);
    if (sock && sock->conn && NETCONNTYPE_GROUP(sock->conn->type)==NETCONN_TCP && sock->conn->pcb.tcp &&
        sock->conn->pcb.tcp->snd_buf==TCP_SND_BUF) { // Idle: nothing queued.
        sock->conn->pcb.tcp->snd_buf=TCP_SND_BUF*3;
        g.ok=true;
    }
    xSemaphoreGive(g.done);
}
static void growSendBuffer(Radio &r) {
    if (r.serialSession || r.sendBufferGrown || r.client.fd()<0) return;
    static StaticSemaphore_t storage;
    static SemaphoreHandle_t done=xSemaphoreCreateBinaryStatic(&storage);
    GrowSend g{r.client.fd(),done,false};
    if (tcpip_callback(growSendInTcpip,&g)==ERR_OK && xSemaphoreTake(done,pdMS_TO_TICKS(100))==pdTRUE)
        r.sendBufferGrown=g.ok;
}
static bool sendBytes(Radio &r,const uint8_t *p,size_t n) {
    uint64_t until=now()+2000000;
    while (n) {
        if ((!r.serialSession && !r.client.connected()) || now()>=until) return false;
        size_t sent=r.io->write(p,n);
        if (sent) { p+=sent; n-=sent; } else delay(1);
    }
    return true;
}
static void failure(Radio &r,uint32_t id,const char *error,const char *phase,bool sent=false) {
    r.io->printf("{\"id\":%lu,\"ok\":false,\"error\":\"%s\",\"phase\":\"%s\",\"command_sent\":%s}\n",
        (unsigned long)id,error,phase,sent?"true":"false");
}
static void hexAppend(String &s,const uint8_t *data,size_t n) {
    const char *digits="0123456789abcdef";
    for (size_t i=0;i<n;++i) { s+=digits[data[i]>>4]; s+=digits[data[i]&15]; }
}
// Wait until an absolute esp_timer deadline. Yield to the RTOS while far from
// the deadline so Wi-Fi and the task watchdog keep running on long gaps; busy-
// spin the final <3 ms so short OOK edges keep microsecond precision.
static void ookWaitUntil(uint64_t deadline_us) {
    for (;;) {
        uint64_t t=now();
        if (t>=deadline_us) return;
        if (deadline_us-t>3000) vTaskDelay(1);
    }
}
static void ookSetup(Radio &r,uint32_t freqHz,uint8_t pa[4],uint8_t power,uint8_t ramp,bool tx) {
    uint8_t cmd[16],f1,f2;
    transfer(r,cmd,nullptr,ook_cmd_standby(cmd,1)); ready(r,20);        // XOSC standby
    if (ook_image_cal_band(freqHz,&f1,&f2)) { transfer(r,cmd,nullptr,ook_cmd_calibrate_image(cmd,f1,f2)); ready(r,50); }
    transfer(r,cmd,nullptr,ook_cmd_set_frequency(cmd,freqHz)); ready(r,20);
    transfer(r,cmd,nullptr,ook_cmd_pkt_type(cmd,1)); ready(r,20);        // GFSK base modem
    if (tx) {
        transfer(r,cmd,nullptr,ook_cmd_pa_config(cmd,pa)); ready(r,20);
        transfer(r,cmd,nullptr,ook_cmd_tx_params(cmd,power,ramp)); ready(r,20);
    }
    transfer(r,cmd,nullptr,ook_cmd_standby(cmd,1)); ready(r,20);        // carrier guaranteed off
}
static void ookTransmit(Radio &r) {
    request_t &q=r.req;
    uint8_t cw[4],sb[4];
    size_t nCw=ook_cmd_tx_cw(cw), nSb=ook_cmd_standby(sb,1);
    BusLock lock;
    if (!ready(r,20)) { failure(r,q.id,"busy_timeout","ook_setup"); return; }
    // Key at the full 16 MHz SPI so a command frame is ~1 µs, not ~32 µs; this
    // is the edge-timing floor. Restored before returning.
    uint32_t savedSpeed=r.speed; r.speed=board::stream_spi;
    ookSetup(r,q.ook_freq_hz,q.ook_pa,q.ook_power,q.ook_ramp,true);
    uint64_t start=now(),sched=start,maxJit=0;
    for (uint32_t rep=0;rep<q.ook_repeat;++rep) {
        for (uint32_t i=0;i<q.ook_pulse_count;++i) {
            // Even index = carrier on (SetTxCw), odd = off (SetStandby).
            if (i&1) transfer(r,sb,nullptr,nSb); else transfer(r,cw,nullptr,nCw);
            uint64_t issued=now(),jit=issued>sched?issued-sched:0;
            if (jit>maxJit) maxJit=jit;
            sched+=ook_pulse_at(q.tx,i);
            ookWaitUntil(sched);
        }
        transfer(r,sb,nullptr,nSb);                 // off during the inter-burst gap
        sched=now()+q.ook_gap_us;
        ookWaitUntil(sched);
    }
    transfer(r,sb,nullptr,nSb); ready(r,20);        // final safety: carrier off
    r.speed=savedSpeed;
    r.io->printf("{\"id\":%lu,\"ok\":true,\"mode\":\"ook_tx\",\"freq_hz\":%lu,\"pulses\":%lu,\"repeats\":%lu,\"total_us\":%llu,\"max_edge_jitter_us\":%llu}\n",
        (unsigned long)q.id,(unsigned long)q.ook_freq_hz,(unsigned long)q.ook_pulse_count,
        (unsigned long)q.ook_repeat,(unsigned long long)(now()-start),(unsigned long long)maxJit);
}
static void ookReceive(Radio &r) {
    request_t &q=r.req;
    uint8_t cmd[16],rssi[2];
    BusLock lock;
    if (!ready(r,20)) { failure(r,q.id,"busy_timeout","ook_setup"); return; }
    uint32_t savedSpeed=r.speed; r.speed=board::stream_spi; // fast RSSI polling
    ookSetup(r,q.ook_freq_hz,q.ook_pa,0,0,false);
    // Wide GFSK RX filter; the bw code sets the detection bandwidth, the rest
    // are placeholders the envelope path does not depend on.
    transfer(r,cmd,nullptr,ook_cmd_gfsk_mod(cmd,4800,0,(uint8_t)q.ook_bw,19000)); ready(r,20);
    transfer(r,cmd,nullptr,ook_cmd_set_rx(cmd,0xffffff)); ready(r,20);  // continuous RX
    String out; out.reserve(16384);
    out="{\"id\":"+String((unsigned long)q.id)+",\"ok\":true,\"mode\":\"ook_rx\",\"freq_hz\":"+
        String((unsigned long)q.ook_freq_hz)+",\"durations_us\":[";
    uint64_t start=now(),endt=start+(uint64_t)q.ook_duration_ms*1000,prev=start,lastEdge=start;
    uint64_t minPoll=UINT64_MAX,maxPoll=0;
    uint32_t polls=0,edges=0; bool level=false,firstLevel=false,first=true;
    while (now()<endt && edges<OOK_MAX_EDGES) {
        transfer(r,cmd,nullptr,ook_cmd_get_rssi(cmd));
        if (!ready(r,5)) break;
        transfer(r,nullptr,rssi,2);                 // status + RSSI byte
        int32_t rssiX2=-(int32_t)rssi[1];           // dBm*2; actual dBm = -rssi/2
        bool lv=rssiX2>=q.ook_threshold_dbm_x2;      // carrier present above threshold
        uint64_t t=now(),dp=t-prev; prev=t; ++polls;
        if (dp<minPoll) minPoll=dp;
        if (dp>maxPoll) maxPoll=dp;
        if (first) { level=lv; firstLevel=lv; lastEdge=t; first=false; }
        else if (lv!=level) { if (edges) out+=","; out+=String((unsigned long)(t-lastEdge)); lastEdge=t; level=lv; ++edges; }
        if (q.ook_poll_us) ookWaitUntil(t+q.ook_poll_us);
    }
    transfer(r,cmd,nullptr,ook_cmd_standby(cmd,1)); ready(r,20);
    r.speed=savedSpeed;
    out+="],\"first_level\":"+String(firstLevel?1:0)+",\"edges\":"+String((unsigned long)edges)+
         ",\"polls\":"+String((unsigned long)polls)+",\"poll_min_us\":"+
         String((unsigned long)(minPoll==UINT64_MAX?0:minPoll))+",\"poll_max_us\":"+
         String((unsigned long)maxPoll)+",\"truncated\":"+String(edges>=OOK_MAX_EDGES?"true":"false")+"}\n";
    r.io->write(reinterpret_cast<const uint8_t *>(out.c_str()),out.length());
}
static void stream(Radio &r) {
    request_t &q=r.req;
    if (otaBusy.load()) { failure(r,q.id,"firmware_update_in_progress","admission"); return; }
    r.packed=q.command==CMD_PACKED;
    if(r.packed && (q.tx_len!=8 || !r.compact || (q.sample_rate!=62500 && q.sample_rate!=125000))) {
        failure(r,q.id,"packed_binding_rate_required","validate");return;
    }
    if(r.packed && r.serialSession && (q.sample_rate==125000 || !r.decimated) && (!q.iterations || q.iterations>128)) {
        failure(r,q.id,"packed_uart_bandwidth_max_128_blocks","admission");return;
    }
    // LIQ1 on CP210x is smoke-only. LID8 fits the 921600-baud UART.
    if (r.serialSession && !r.compact && (!q.iterations || q.iterations>32)) {
        failure(r,q.id,"serial_capture_max_32_blocks","validate"); return;
    }
    r.rate=q.command==CMD_STREAM?125000:q.sample_rate; r.blocks=q.iterations; r.jobId=q.id;
    if (q.pause_ms || r.speed!=board::stream_spi || !validate(r)) {
        failure(r,q.id,"stream_configuration","validate"); return;
    }
    xSemaphoreTake(streamMutex,portMAX_DELAY);
    Radio &other=radios[1-r.index];
    if (other.active.load() && r.rate+other.rate>125000) {
        xSemaphoreGive(streamMutex);
        failure(r,q.id,"dual_rate_max_62500_each","admission"); return;
    }
    // Reuse the same storage budget with smaller queue items for network modes.
    // This gives /2 streaming about 400 ms of radio-side jitter headroom.
    size_t itemBytes=r.compact?offsetof(Frame,bytes)+IQ_HEADER_BYTES+
        (r.decimated?127u:COMPACT_PAYLOAD_BYTES):sizeof(Frame);
    r.queueCapacity=board::queue_frames*sizeof(Frame)/itemBytes;
    r.queue=xQueueCreateStatic(r.queueCapacity,itemBytes,r.queueStorage,&r.queueControl);
    if(!r.queue)abort();
    memset(&r.decimator,0,sizeof(r.decimator));
    r.flags=r.produced=r.readOps=r.maxQueue=r.maxBlock=0;
    r.sent.store(0);r.elapsed=0;
    r.streamCompact=r.compact;r.streamDecimated=r.decimated;r.streamPacked=r.packed;r.streamRate=r.rate;
    r.start=0; r.stop.store(false); r.done.store(false);
    growSendBuffer(r);
    r.io->printf("{\"id\":%lu,\"ok\":true,\"stream\":\"%s\",\"format\":\"%s\",\"acquisition\":\"%s\",\"header_bytes\":44,\"samples_per_frame\":%u,\"native_samples_per_frame\":125,\"decimation\":%u,\"blocks\":%lu,\"tcp_send_buffer\":%u}\n",
        (unsigned long)q.id,r.decimated?"LID8":r.compact?"LIQ8":"LIQ1",r.compact?"iq8_block_float":"iq23le",r.packed?"arc8":"raw23",r.decimated?0u:125u,r.decimated?2u:1u,(unsigned long)r.blocks,r.serialSession?0u:unsigned(TCP_SND_BUF*(r.sendBufferGrown?3:1)));
    r.active.store(true);
    xSemaphoreGive(streamMutex);
    Frame frame;
    bool connected=true;
    uint32_t sent=0;
    size_t groupBytes=0;
    uint64_t dataSent=0;
    uint64_t lastIndicators=0;
    while (!r.done.load() || uxQueueMessagesWaiting(r.queue) || groupBytes) {
        // USB stream() runs inside loop(), so keep thermal control and status
        // LEDs alive here. TCP sessions leave loop() free to do this itself.
        if (r.serialSession && now()-lastIndicators>=20000) {
            updateIndicators(); lastIndicators=now();
        }
        while (r.io->available()) if (r.io->read()==3) r.stop.store(true);
        if ((!r.serialSession && !r.client.connected())) { connected=false; r.stop.store(true); }
        if (xQueueReceive(r.queue,&frame,pdMS_TO_TICKS(5))==pdTRUE) {
            uint8_t *wire=frame.bytes;
            size_t payload=frame.payloadBytes,frameBytes=IQ_HEADER_BYTES+payload;
            iq_header_rate(wire,1,0,r.jobId,frame.sequence,frame.samples,payload,
                frame.timestamp,iq_crc32(wire+IQ_HEADER_BYTES,payload),r.decimated?r.rate/2:r.rate);
            if (r.compact) {
                memcpy(wire,r.decimated?"LID8":"LIQ8",4);
                if(r.decimated)iq_le32(wire+16,uint32_t((uint64_t(frame.sequence)*125+1)/2));
                iq_le32(wire+40,iq_crc32(wire,40));
            }
            if (r.serialSession) {
                if (connected && !sendBytes(r,wire,frameBytes)) { connected=false; r.stop.store(true); }
                if (connected) ++sent;
            } else {
                // Fill complete TCP segments; LIQ1 frames may span writes.
                // Avoid flushing a short trailing segment after every group.
                memcpy(r.networkGroup+groupBytes,wire,frameBytes);
                groupBytes+=frameBytes;
            }
        }
        size_t flushBytes=groupBytes>=4*TCP_MSS?4*TCP_MSS:
            (r.done.load() && !uxQueueMessagesWaiting(r.queue)?groupBytes:0);
        if (flushBytes) {
            if (connected && !sendBytes(r,r.networkGroup,flushBytes)) { connected=false; r.stop.store(true); }
            if (connected) {
                dataSent+=flushBytes;
                // /2 data frames alternate 171 and 169 bytes, 340 per pair.
                sent=r.decimated?uint32_t(dataSent/340)*2u+(dataSent%340>=171):
                    uint32_t(dataSent/(IQ_HEADER_BYTES+(r.compact?COMPACT_PAYLOAD_BYTES:IQ_PAYLOAD_BYTES)));
            }
            groupBytes-=flushBytes;
            memmove(r.networkGroup,r.networkGroup+flushBytes,groupBytes);
        }
        r.sent.store(sent);
    }
    // done is the synchronization point; producer no longer accesses this job.
    xSemaphoreTake(streamMutex,portMAX_DELAY);
    r.active.store(false);
    r.elapsed=now()-r.start;
    xSemaphoreGive(streamMutex);
    if (!connected) r.flags|=IQ_USB_LOST; // Existing LIQ1 bit means transport lost.
    uint8_t end[IQ_HEADER_BYTES+36], *stats=end+IQ_HEADER_BYTES;
    uint32_t values[]={r.produced,sent,r.maxQueue,r.readOps,r.maxBlock,r.speed,
        uint32_t(now()-r.start),r.flags&(IQ_DMA_STARVED|IQ_QUEUE_FULL|IQ_USB_LOST|IQ_RADIO_ERROR|IQ_FORMAT_ERROR)?0xffffffffu:0,r.blocks};
    for (unsigned i=0;i<9;++i) iq_le32(stats+4*i,values[i]);
    iq_header_rate(end,2,r.flags,q.id,r.produced,0,36,uint32_t(now()-r.start),iq_crc32(stats,36),r.decimated?r.rate/2:r.rate);
    if (r.compact) {
        memcpy(end,r.decimated?"LID8":"LIQ8",4);
        if(r.decimated)iq_le32(end+16,uint32_t((uint64_t(r.produced)*125+1)/2));
        iq_le32(end+40,iq_crc32(end,40));
    }
    if (connected && !sendBytes(r,end,sizeof(end))) connected=false;
    if (!connected) r.client.stop();
}
static bool hexToBytes(const char *s,uint8_t *out,size_t cap,size_t *n) {
    size_t len=strlen(s);
    if ((len&1) || len/2>cap) return false;
    for (size_t i=0;i<len;++i) {
        char c=s[i]; int v=c>='0'&&c<='9'?c-'0':c>='a'&&c<='f'?c-'a'+10:c>='A'&&c<='F'?c-'A'+10:-1;
        if (v<0) return false;
        if (!(i&1)) out[i/2]=uint8_t(v<<4); else out[i/2]|=uint8_t(v);
    }
    *n=len/2; return true;
}
static bool parseU32(const char *s,uint32_t lo,uint32_t hi,uint32_t *out) {
    if (!s || !*s) return false;
    char *end; unsigned long v=strtoul(s,&end,10);
    if (*end || v<lo || v>hi) return false;
    *out=uint32_t(v); return true;
}
// ESP32-only OOK commands, parsed ahead of the shared parse_request so the Pico
// grammar is untouched. ooktx keys a carrier; ookrx recovers an RSSI envelope.
static bool parseOok(char *line,request_t *req,const char **error) {
    const char *p=line; char tok[64];
    if (!nextToken(p,tok,sizeof(tok)) || !parseU32(tok,1,UINT32_MAX,&req->id)) return false;
    if (!nextToken(p,tok,sizeof(tok))) return false;
    bool tx=!strcmp(tok,"ooktx"), rx=!strcmp(tok,"ookrx");
    if (!tx && !rx) return false;
    uint32_t id=req->id; memset(req,0,sizeof(*req)); req->id=id;
    // Full synthesizer span; hardware rejects anything its matching cannot pass.
    if (!nextToken(p,tok,sizeof(tok)) || !parseU32(tok,150000000u,2500000000u,&req->ook_freq_hz)) {
        *error="ook_freq_150M_to_2500M"; return false;
    }
    if (tx) {
        req->command=CMD_OOKTX;
        char paname[8]; if (!nextToken(p,paname,sizeof(paname))) { *error="ook_pa"; return false; }
        int pi=ook_pa_index(paname);
        if (pi<0) { *error="ook_pa_lp_hp_hf"; return false; }
        const ook_pa_profile_t *prof=ook_pa_profile((unsigned)pi);
        memcpy(req->ook_pa,prof->config,4);
        if (!nextToken(p,tok,sizeof(tok))) { *error="ook_power"; return false; }
        char *end; long power=strtol(tok,&end,10);
        if (*end || power<prof->min_dbm || power>prof->max_dbm) { *error="ook_power_out_of_pa_range"; return false; }
        req->ook_power=uint8_t(power&0xff);
        uint32_t ramp; if (!nextToken(p,tok,sizeof(tok)) || !parseU32(tok,0,15,&ramp)) { *error="ook_ramp_0_to_15"; return false; }
        req->ook_ramp=uint8_t(ramp);
        if (!nextToken(p,tok,sizeof(tok)) || !parseU32(tok,1,10000,&req->ook_repeat)) { *error="ook_repeat_1_to_10000"; return false; }
        if (!nextToken(p,tok,sizeof(tok)) || !parseU32(tok,0,OOK_MAX_PULSE_US,&req->ook_gap_us)) { *error="ook_gap_0_to_32768"; return false; }
        char pulses[2*OOK_MAX_PULSES+1]; size_t nbytes;
        if (!nextToken(p,pulses,sizeof(pulses)) || !hexToBytes(pulses,req->tx,BRIDGE_MAX_BYTES,&nbytes) ||
            (nbytes&1) || nbytes<2) { *error="ook_pulses_hex_be16"; return false; }
        req->ook_pulse_count=uint32_t(nbytes/2);
        // Bound total on-air time; reject a pulse of zero or over the ceiling.
        uint64_t one=0;
        for (uint32_t i=0;i<req->ook_pulse_count;++i) {
            uint16_t d=ook_pulse_at(req->tx,i);
            if (d==0 || d>OOK_MAX_PULSE_US) { *error="ook_pulse_1_to_32768_us"; return false; }
            one+=d;
        }
        if ((one+req->ook_gap_us)*req->ook_repeat>OOK_MAX_TX_TOTAL_US) { *error="ook_total_tx_time_over_2s"; return false; }
        if (nextToken(p,tok,sizeof(tok))) { *error="ook_trailing_token"; return false; }
        return true;
    }
    req->command=CMD_OOKRX;
    if (!nextToken(p,tok,sizeof(tok)) || !parseU32(tok,0,31,&req->ook_bw)) { *error="ook_bw_code_0_to_31"; return false; }
    if (!nextToken(p,tok,sizeof(tok)) || !parseU32(tok,1,OOK_MAX_RX_MS,&req->ook_duration_ms)) { *error="ook_duration_1_to_30000_ms"; return false; }
    if (!nextToken(p,tok,sizeof(tok))) { *error="ook_threshold"; return false; }
    char *end; long thr=strtol(tok,&end,10);
    if (*end || thr>0 || thr<-400) { *error="ook_threshold_dbm_x2_-400_to_0"; return false; }
    req->ook_threshold_dbm_x2=int32_t(thr);
    if (!nextToken(p,tok,sizeof(tok)) || !parseU32(tok,OOK_MIN_POLL_US,100000,&req->ook_poll_us)) { *error="ook_poll_5_to_100000_us"; return false; }
    // RX reuses the LP PA slot for setup symmetry; it is not keyed in RX.
    memcpy(req->ook_pa,ook_pa_profile(0)->config,4);
    if (nextToken(p,tok,sizeof(tok))) { *error="ook_trailing_token"; return false; }
    return true;
}
static bool parseNomadRequest(char *line,request_t *req,bool *compact,bool *decimated,const char **error) {
    *compact=false;*decimated=false;
    {
        unsigned long pid=0; char pcmd[16];
        if (sscanf(line,"%lu %15s",&pid,pcmd)==2 && (!strcmp(pcmd,"ooktx")||!strcmp(pcmd,"ookrx"))) {
            *error="ook_syntax";
            return parseOok(line,req,error);
        }
    }
    unsigned long id=0,rate=0,blocks=0;
    char command[16],extra;
    if(sscanf(line,"%lu %15s",&id,command)==2 && (!strcmp(command,"streamp8") || !strcmp(command,"streampd"))) {
        char uid[18]={0};uint8_t decoded[8];
        memset(req,0,sizeof(*req));req->id=id;
        if(sscanf(line,"%lu %15s %lu %lu %17s %c",&id,command,&rate,&blocks,uid,&extra)!=5 || strlen(uid)!=16) {
            *error="streamp_rate_blocks_uid";return false;
        }
        for(unsigned i=0;i<16;++i) {
            char c=uid[i];int nibble=c>='0'&&c<='9'?c-'0':c>='a'&&c<='f'?c-'a'+10:c>='A'&&c<='F'?c-'A'+10:-1;
            if(nibble<0){*error="packed_uid_hex";return false;}
            if(!(i&1))decoded[i/2]=uint8_t(nibble<<4);else decoded[i/2]|=uint8_t(nibble);
        }
        char normalized[96];snprintf(normalized,sizeof(normalized),"%lu stream2 %lu %lu",id,rate,blocks);
        if(!parse_request(normalized,req,error))return false;
        req->command=CMD_PACKED;req->tx_len=8;memcpy(req->tx,decoded,8);
        *compact=true;*decimated=!strcmp(command,"streampd");return true;
    }
    if (sscanf(line,"%lu %15s",&id,command)==2 && (!strcmp(command,"stream8") || !strcmp(command,"streamd"))) {
        if (sscanf(line,"%lu %15s %lu %lu %c",&id,command,&rate,&blocks,&extra)!=4) {
            memset(req,0,sizeof(*req)); req->id=id; *error="stream8_rate_blocks"; return false;
        }
        // Native narrow rate (LR1121 BW 0x0E): LIQ8 only, no ESP32 FIR stage.
        bool narrow=rate==31250 && !strcmp(command,"stream8");
        char normalized[96];
        snprintf(normalized,sizeof(normalized),"%lu stream2 %lu %lu",id,narrow?62500ul:rate,blocks);
        if (!parse_request(normalized,req,error)) return false;
        if (narrow) req->sample_rate=31250;
        *compact=true;*decimated=!strcmp(command,"streamd");return true;
    }
    return parse_request(line,req,error);
}
static void request(Radio &r) {
    request_t &q=r.req;
    switch(q.command) {
    case CMD_INFO:
        r.io->printf("{\"id\":%lu,\"ok\":true,\"bridge\":\"lr1121-spi\",\"protocol\":1,\"firmware\":\"nomad-0.11.0\",\"board\":\"%s\",\"rf_switch\":\"%s\",\"radio\":%u,\"radios\":2,\"stream\":\"LIQ1\",\"compact_stream\":\"LIQ8\",\"decimated_stream\":\"LID8\",\"transport\":\"%s\",\"uart_baud\":%lu,\"usb_compact_stream\":true,\"experimental_packed_stream\":true,\"serial_capture_max_blocks\":%u,\"spi_hz\":%lu,\"max_bytes\":4096,\"reset_guard\":0,\"pins\":{\"sck\":25,\"miso\":33,\"mosi\":32,\"cs\":%d,\"busy\":%d,\"reset\":%d,\"dio9\":%d,\"fan\":%d,\"led_rgb\":%d,\"led_count\":%u}}\n",
            (unsigned long)q.id,board::name,board::rf_switch,r.index,r.serialSession?"uart":"tcp",(unsigned long)board::uart_baud,r.serialSession?32u:0u,(unsigned long)r.speed,board::cs[r.index],board::busy[r.index],board::reset[r.index],board::dio9[r.index],board::fan,board::led,board::led_count); return;
    case CMD_PINS:
        r.io->printf("{\"id\":%lu,\"ok\":true,\"cs\":%d,\"reset\":%d,\"busy\":%d,\"dio9\":%d}\n",(unsigned long)q.id,
            digitalRead(board::cs[r.index]),digitalRead(board::reset[r.index]),digitalRead(board::busy[r.index]),digitalRead(board::dio9[r.index])); return;
    case CMD_SPEED:
        if (q.speed_hz>board::stream_spi) { failure(r,q.id,"speed_max_16000000","validate"); return; }
        // ESP32 APB divides 80 MHz: unlike Pico, 16 MHz is exactly achievable.
        r.speed=spiClockDivToFrequency(spiFrequencyToClockDiv(q.speed_hz));
        r.io->printf("{\"id\":%lu,\"ok\":true,\"spi_hz\":%lu}\n",(unsigned long)q.id,(unsigned long)r.speed); return;
    case CMD_RESET:
        resetRadio(r);
        if (!ready(r,q.timeout_ms)) { failure(r,q.id,"busy_timeout","after_reset"); return; }
        break;
    case CMD_WAIT:
        if (!ready(r,q.timeout_ms)) { failure(r,q.id,"busy_timeout","wait"); return; }
        break;
    case CMD_STREAM: case CMD_STREAM2: case CMD_PACKED: stream(r); return;
    case CMD_OOKTX: ookTransmit(r); return;
    case CMD_OOKRX: ookReceive(r); return;
    case CMD_READ: case CMD_WRITE: case CMD_XFER: {
        String out; out.reserve(2*(q.tx_len+q.read_len)+200);
        uint8_t *cmdRx=static_cast<uint8_t *>(malloc(q.tx_len));
        if (!cmdRx) { failure(r,q.id,"out_of_memory","command"); return; }
        {
            BusLock lock;
            if (!ready(r,q.timeout_ms)) { free(cmdRx); failure(r,q.id,"busy_timeout","before_command"); return; }
            transfer(r,q.tx,cmdRx,q.tx_len);
            delayMicroseconds(95);
            if (q.command!=CMD_XFER && !ready(r,q.timeout_ms)) { free(cmdRx); failure(r,q.id,"busy_timeout","after_command",true); return; }
            if (q.command==CMD_READ) transfer(r,nullptr,r.rx,q.read_len+1);
        }
        out="{\"id\":"+String(q.id)+",\"ok\":true,";
        if (q.command==CMD_READ) {
            out+="\"status\":"+String(r.rx[0])+",\"cmd_status\":"+String(r.rx[0]>>1)+",\"data\":\"";
            hexAppend(out,r.rx+1,q.read_len); out+="\",\"command_rx\":\"";
        } else out+="\"rx\":\"";
        hexAppend(out,cmdRx,q.tx_len); free(cmdRx); out+="\"}\n";
        if (!sendBytes(r,reinterpret_cast<const uint8_t *>(out.c_str()),out.length())) r.client.stop();
        return;
    }
    default: failure(r,q.id,"unsupported_on_nomad","command"); return;
    }
    r.io->printf("{\"id\":%lu,\"ok\":true,\"busy\":%d}\n",(unsigned long)q.id,digitalRead(board::busy[r.index]));
}
static void serve(void *arg) {
    Radio &r=*static_cast<Radio *>(arg);
    for (;;) {
        r.client=servers[r.index].available(); r.sendBufferGrown=false;
        if (!r.client) { delay(10); continue; }
        if (xSemaphoreTake(r.owner,0)!=pdTRUE) { r.client.stop(); continue; }
        r.io=&r.client; r.serialSession=false;
        r.client.setNoDelay(true); r.client.setTimeout(2);
        size_t used=0; bool discard=false; uint64_t last=now();
        while (r.client.connected()) {
            // Reject additional owners rather than leaving stale sessions queued.
            WiFiClient extra=servers[r.index].available(); if (extra) extra.stop();
            if (!r.io->available()) {
                if (used && now()-last>2000000) { used=0; discard=true; }
                delay(1); continue;
            }
            int c=r.io->read(); last=now(); if (c=='\r') continue;
            if (c=='\n') {
                if (discard) failure(r,0,"invalid_or_incomplete_line","parse");
                else if (used) {
                    r.line[used]=0; const char *error;
                    if (networkRequest(r.line,*r.io,false)) {}
                    else if (parseNomadRequest(r.line,&r.req,&r.compact,&r.decimated,&error)) request(r);
                    else failure(r,r.req.id,error,"parse");
                }
                used=0; discard=false;
            } else if (!discard) {
                if ((c<32 && c!='\t') || c>126 || used+1>=sizeof(r.line)) discard=true;
                else r.line[used++]=char(c);
            }
        }
        r.client.stop(); resetRadio(r); // Release only this radio on disconnect.
        r.speed=500000;
        xSemaphoreGive(r.owner);
    }
}
static void otaBegin() {
#ifdef NOMAD_OTA_PASSWORD
    // Wi-Fi firmware update (espota, UDP 3232). Compiled in only when a local
    // password is defined in wifi.local.h; refused while any radio streams.
    static bool started=false;
    if (started) return;
    ArduinoOTA.setHostname(board::hostname);
    ArduinoOTA.setPassword(NOMAD_OTA_PASSWORD);
    ArduinoOTA.setMdnsEnabled(false);
    ArduinoOTA.setRebootOnSuccess(true);
    ArduinoOTA.onStart([]{ otaBusy.store(true); });
    ArduinoOTA.onError([](ota_error_t){ otaBusy.store(false); });
    ArduinoOTA.begin();
    started=true;
#endif
}
// "<id> fwupdate SIZE MD5HEX" over UART: firmware update with no boot button,
// usable whenever the app runs (also in safe mode, and when Wi-Fi is down).
// After the "ready" reply the host sends SIZE raw bytes in 4096-byte chunks and
// waits for {"offset":N} after each. MD5 is checked before the new slot is made
// bootable; any failure leaves the running image untouched.
static bool serialFirmwareUpdate(const char *line) {
    unsigned long id=0,size=0; char md5[33]={0},extra;
    if (sscanf(line,"%lu fwupdate %lu %32s %c",&id,&size,md5,&extra)!=3) return false;
    auto fail=[&](const char *error) {
        Update.abort();
        Serial.printf("{\"id\":%lu,\"ok\":false,\"error\":\"%s\",\"phase\":\"fwupdate\"}\n",id,error);
        return true;
    };
    if (radios[0].active.load() || radios[1].active.load()) return fail("streaming");
    if (strlen(md5)!=32 || !size) return fail("size_md5");
    if (!Update.begin(size)) return fail(Update.errorString());
    if (!Update.setMD5(md5)) return fail("md5");
    Serial.printf("{\"id\":%lu,\"ok\":true,\"ready\":true,\"chunk\":4096}\n",id);
    static uint8_t chunk[4096];
    size_t offset=0;
    while (offset<size) {
        size_t want=std::min(size_t(size-offset),sizeof(chunk)),got=0;
        uint64_t deadline=now()+5000000;
        while (got<want) {
            size_t n=Serial.read(chunk+got,want-got);
            if (n) { got+=n; deadline=now()+5000000; }
            else if (now()>deadline) return fail("timeout");
            else delay(1);
        }
        if (Update.write(chunk,want)!=want) return fail(Update.errorString());
        offset+=want;
        Serial.printf("{\"id\":%lu,\"ok\":true,\"offset\":%u}\n",id,unsigned(offset));
    }
    if (!Update.end()) return fail(Update.errorString());
    Serial.printf("{\"id\":%lu,\"ok\":true,\"done\":true,\"rebooting\":true}\n",id);
    Serial.flush(); delay(100); ESP.restart();
    return true;
}
static bool serialStarted=false, networkStartedOnce=false;
// A start-up failure must not reboot-loop: that would also take Wi-Fi updates
// down and force a boot-button reflash. Stay on the network, report why, and
// keep accepting a fixed image over OTA.
[[noreturn]] static void safeMode(const char *why) {
    if (!serialStarted) { Serial.begin(board::uart_baud); serialStarted=true; }
    if (!networkStartedOnce) { loadWifiCredentials(); startNetwork(); networkStartedOnce=true; }
    otaBegin();
    static char line[128]; size_t used=0;
    for (;;) {
        Serial.printf("SAFE MODE: %s; free heap %u, largest block %u\n",why,ESP.getFreeHeap(),
            unsigned(heap_caps_get_largest_free_block(MALLOC_CAP_8BIT)));
        for (unsigned i=0;i<100;++i) {
            while (Serial.available()) { // Accept only fwupdate here.
                int c=Serial.read();
                if (c=='\n') { line[used]=0; used=0; serialFirmwareUpdate(line); }
                else if (c!='\r' && used+1<sizeof(line)) line[used++]=char(c);
            }
            networkTick();
#ifdef NOMAD_OTA_PASSWORD
            ArduinoOTA.handle();
#endif
            delay(10);
        }
    }
}
#define REQUIRE(condition,why) do { if (!(condition)) safeMode(why); } while (0)
void setup() {
    // A maximum ASCII SPI command is over 8 KiB. The default 256-byte UART
    // buffer can overflow while loop() updates indicators at 921600 baud.
    REQUIRE(Serial.setRxBufferSize(BRIDGE_LINE_SIZE) >= BRIDGE_LINE_SIZE,"uart rx buffer");
    // Queue complete IQ frames instead of waiting on the 128-byte hardware
    // FIFO for every write. Per-frame blocking otherwise leaves gaps on TX.
    REQUIRE(Serial.setTxBufferSize(4096) >= 4096,"uart tx buffer");
    Serial.begin(board::uart_baud); serialStarted=true;
    spiMutex=xSemaphoreCreateMutex();
    streamMutex=xSemaphoreCreateMutex();
    REQUIRE(spiMutex && streamMutex,"mutex");
    // Keep the external PA control at zero; hold backpack in reset so its
    // original ELRS auxiliary firmware cannot interfere with this experiment.
    // Boards without these pins (XR4) use -1; GPIO26 there is radio 0 RESET.
    if (board::pa_apc>=0) { pinMode(board::pa_apc,OUTPUT); digitalWrite(board::pa_apc,LOW); }
    if (board::backpack_en>=0) { pinMode(board::backpack_en,OUTPUT); digitalWrite(board::backpack_en,LOW); }
    indicatorsBegin();
    for (unsigned i=0;i<2;++i) {
        Radio &r=radios[i]; r.index=i;
        r.owner=xSemaphoreCreateMutex();
        REQUIRE(r.owner,"radio mutex");
        digitalWrite(board::cs[i],HIGH); pinMode(board::cs[i],OUTPUT);
        digitalWrite(board::reset[i],LOW); pinMode(board::reset[i],OUTPUT);
        pinMode(board::busy[i],INPUT); pinMode(board::dio9[i],INPUT);
        r.queueStorage=static_cast<uint8_t *>(malloc(board::queue_frames*sizeof(Frame)));
        REQUIRE(r.queueStorage,"send queue memory");
        r.queueCapacity=board::queue_frames;
        r.queue=xQueueCreateStatic(board::queue_frames,sizeof(Frame),r.queueStorage,&r.queueControl);
        REQUIRE(r.queue,"send queue");
    }
    radioSPI.begin(board::sck,board::miso,board::mosi,-1);
    // Sets full duplex, mode 0, MSB first once; transfer() then only writes the clock.
    radioSPI.beginTransaction(SPISettings(500000,MSBFIRST,SPI_MODE0)); radioSPI.endTransaction();
    for (Radio &r:radios) resetRadio(r);
    loadWifiCredentials();
    startNetwork(); networkStartedOnce=true;
    otaBegin(); // Before anything else can fail, so a bad image stays fixable.
    for (unsigned i=0;i<2;++i) {
        servers[i].begin(); servers[i].setNoDelay(true);
        REQUIRE(xTaskCreatePinnedToCore(serve,i?"radio1":"radio0",8192,&radios[i],2,nullptr,0)==pdPASS,"serve task");
    }
    REQUIRE(xTaskCreatePinnedToCore(acquire,"acquire",4096,nullptr,3,&acquireTask,1)==pdPASS,"acquire task");
    Serial.printf("%s SDR: %s mode, TCP 1121 / 1122; use '<id> wifi' for IP/status; free heap %u, largest block %u\n",
        board::name,staEnabled()?"STA":"AP",ESP.getFreeHeap(),unsigned(heap_caps_get_largest_free_block(MALLOC_CAP_8BIT)));
}
void loop() {
    networkTick();
#ifdef NOMAD_OTA_PASSWORD
    if (!radios[0].active.load() && !radios[1].active.load()) ArduinoOTA.handle();
#endif
    updateIndicators();
    static char consoleLine[BRIDGE_LINE_SIZE];
    static request_t consoleReq;
    static bool consoleCompact=false;
    static bool consoleDecimated=false;
    static unsigned selectedRadio=0;
    static size_t used=0;
    static bool discard=false;
    static uint64_t last=0;
    if (used && now()-last>2000000) { used=0; discard=true; }
    while (Serial.available()) {
        int c=Serial.read(); last=now();
        if (c=='\r') continue;
        if (c=='\n') {
            if (discard) Serial.println("{\"id\":0,\"ok\":false,\"error\":\"invalid_or_incomplete_line\",\"phase\":\"parse\"}");
            else if (used) {
                consoleLine[used]=0;
                unsigned long id=0; unsigned index=0; char trailing=0;
                if (serialFirmwareUpdate(consoleLine)) {}
                else if (networkRequest(consoleLine,Serial,true)) {}
                else if (sscanf(consoleLine,"%lu radio %u %c",&id,&index,&trailing)==2 && id>0 && index<2) {
                    // Selection alone does not issue SPI or reset either chip.
                    selectedRadio=index;
                    Serial.printf("{\"id\":%lu,\"ok\":true,\"radio\":%u}\n",id,index);
                } else {
                    const char *error;
                    if (!parseNomadRequest(consoleLine,&consoleReq,&consoleCompact,&consoleDecimated,&error)) {
                        Serial.printf("{\"id\":%lu,\"ok\":false,\"error\":\"%s\",\"phase\":\"parse\"}\n",(unsigned long)consoleReq.id,error);
                    } else {
                        Radio &r=radios[selectedRadio];
                        if (xSemaphoreTake(r.owner,0)!=pdTRUE) {
                            Serial.printf("{\"id\":%lu,\"ok\":false,\"error\":\"radio_owned_by_tcp\",\"phase\":\"admission\"}\n",(unsigned long)consoleReq.id);
                        } else {
                            r.req=consoleReq; r.compact=consoleCompact;r.decimated=consoleDecimated;r.io=&Serial; r.serialSession=true;
                            request(r);
                            Serial.flush();
                            r.io=&r.client; r.serialSession=false;
                            xSemaphoreGive(r.owner);
                        }
                    }
                }
            }
            used=0; discard=false;
        } else if (!discard) {
            if ((c<32 && c!='\t') || c>126 || used+1>=sizeof(consoleLine)) discard=true;
            else consoleLine[used++]=char(c);
        }
    }
    delay(1);
}
