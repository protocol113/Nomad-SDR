/* SPDX-License-Identifier: MIT */
#include "compact.h"
#ifdef ESP_PLATFORM
/* Per-bank hot path: keep it in internal RAM so Wi-Fi activity on the other
   core cannot evict it from the shared flash cache, and optimize for speed. */
#include "esp_attr.h"
#define HOT
#define HOT_DATA
#else
#define HOT
#define HOT_DATA
#endif

static HOT int32_t component(const uint8_t *p) {
    uint32_t v=p[0]|(uint32_t)p[1]<<8|(uint32_t)p[2]<<16;
    return (int32_t)(v&0x3fffffu)-(int32_t)(v&0x400000u);
}
static HOT bool pack_components(uint8_t *dst,const uint8_t *src,unsigned count) {
    uint32_t peak=0;
    for (unsigned i=0;i<count;++i) {
        if (src[3*i+2]&0x80) return false;
        int32_t v=component(src+3*i);
        uint32_t magnitude=(uint32_t)(v<0?-v:v);
        if (magnitude>peak) peak=magnitude;
    }
    unsigned shift=0;
    while (peak>(127u<<shift)) ++shift;
    dst[0]=(uint8_t)shift;
    for (unsigned i=0;i<count;++i) {
        int32_t v=component(src+3*i);
        uint32_t magnitude=(uint32_t)(v<0?-v:v);
        if (shift) magnitude=(magnitude+(1u<<(shift-1)))>>shift;
        int32_t value=v<0?-(int32_t)magnitude:(int32_t)magnitude;
        dst[i+1]=(uint8_t)value;
    }
    return true;
}
bool nomad_pack8(uint8_t *dst,const uint8_t *src) {
    return pack_components(dst,src,250);
}
bool nomad_arc8(uint8_t *dst,const uint8_t *src,uint32_t sequence) {
    uint32_t marker=(uint32_t)src[0]<<24|(uint32_t)src[1]<<16|(uint32_t)src[2]<<8|src[3];
    unsigned shift=(marker>>24)&127;
    if(!(marker&0x80000000u) || shift>16 || (marker&0xffffffu)!=((sequence+1)&0xffffffu))return false;
    dst[0]=(uint8_t)shift;
    for(unsigned i=0;i<250;++i) {
        unsigned offset=i+4;
        dst[i+1]=src[(offset&~3u)+3-(offset&3)];
    }
    return true;
}
bool nomad_expand8(uint8_t *dst,const uint8_t *src) {
    if(src[0]>16)return false;
    for(unsigned i=0;i<250;++i) {
        int32_t value=(int32_t)(int8_t)src[i+1]*(int32_t)(1u<<src[0]);
        if(value>4194303)value=4194303;
        if(value< -4194304)value= -4194304;
        uint32_t bits=(uint32_t)value&0x7fffffu;
        dst[3*i]=(uint8_t)bits;dst[3*i+1]=(uint8_t)(bits>>8);dst[3*i+2]=(uint8_t)(bits>>16);
    }
    return true;
}

/* firwin(33, .4), Hamming window, Q15, exact unity DC gain. */
static HOT_DATA const int16_t taps[33]={50,0,-81,-74,110,256,0,-496,-413,551,1183,0,
    -2144,-1884,2956,9824,13092,9824,2956,-1884,-2144,0,1183,551,-413,-496,
    0,256,110,-74,-81,0,50};
static HOT void encode(uint8_t *p,int64_t sum) {
    /* Round half away from zero, /32768: identical to the division form for
       both signs, without the ESP32's software 64-bit divide. */
    int64_t value=sum<0?-((-sum+16384)>>15):(sum+16384)>>15;
    if(value>4194303)value=4194303;
    if(value< -4194304)value= -4194304;
    uint32_t v=(uint32_t)value&0x7fffff;
    p[0]=(uint8_t)v;p[1]=(uint8_t)(v>>8);p[2]=(uint8_t)(v>>16);
}
/* Symmetric taps: fold mirrored pairs and skip zero taps (14 multiplies per
   channel instead of 33). Sums are exact, so output is unchanged. */
static HOT int64_t fir(const int32_t *w) {
    int64_t sum=(int64_t)w[16]*taps[16];
    static HOT_DATA const uint8_t used[13]={0,2,3,4,5,7,8,9,10,12,13,14,15};
    for(unsigned k=0;k<13;++k) {
        unsigned t=used[k];
        sum+=(int64_t)(w[t]+w[32-t])*taps[t];
    }
    return sum;
}
HOT unsigned nomad_decimate8(uint8_t *dst,const uint8_t *src,nomad_decimator_t *state) {
    uint8_t filtered[63*6];
    unsigned count=0;
    for(unsigned n=0;n<125;++n) {
        const uint8_t *sample=src+n*6;
        if((sample[2]|sample[5])&0x80)return 0;
        /* Doubled history: the 33-sample window ending at position is
           contiguous at [position+1, position+33], oldest first. */
        unsigned position=state->position;
        int32_t i=component(sample),q=component(sample+3);
        state->i[position]=state->i[position+33]=i;
        state->q[position]=state->q[position+33]=q;
        if(!state->phase) {
            encode(filtered+count*6,fir(state->i+position+1));
            encode(filtered+count*6+3,fir(state->q+position+1));
            ++count;
        }
        state->phase^=1;
        state->position=position==32?0:position+1;
    }
    return pack_components(dst,filtered,count*2)?count:0;
}
