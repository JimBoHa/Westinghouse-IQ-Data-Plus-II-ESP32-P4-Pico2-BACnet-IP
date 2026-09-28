/* Port of live/decode_meter.py and live/decode_diagnostics.py at 1add860.
 * Values remain in source units until the BACnet snapshot applies CSV scale. */
#include "iq_model.h"
#include <math.h>
#include <string.h>

const char *iq_kind_name(iq_kind_t kind)
{
    static const char *names[] = {"all_standard", "flags", "settings", "trip"};
    return kind < IQ_KIND_COUNT ? names[kind] : "invalid";
}

uint32_t iq_request_payload(iq_kind_t kind, uint16_t address)
{
    static const uint8_t sub[] = {3, 8, 9, 10};
    return kind < IQ_KIND_COUNT && address <= 4095 ?
        3u | (kind == IQ_STANDARD ? 0 : 0xc0u) | ((uint32_t)address << 8) |
        ((uint32_t)sub[kind] << 20) : UINT32_MAX;
}

bool iq_impacc(uint32_t payload, double *value)
{
    if (payload > 0xffffff || !(payload & 0x400000) || !value) return false;
    unsigned scale = payload >> 16;
    int mantissa = payload & 0xffff;
    if ((scale & 0x80) && (mantissa & 0x8000)) mantissa -= 65536;
    int exponent = scale & 31;
    if (exponent & 16) exponent -= 32;
    *value = mantissa * pow(scale & 32 ? 10.0 : 2.0, exponent);
    return isfinite(*value);
}

static void put(iq_value_t *out, iq_key_t key, double value, bool valid)
{
    out[key].value = value;
    out[key].valid = valid && isfinite(value);
}

static const iq_key_t live_keys[16] = {
    IQ_IA, IQ_IB, IQ_IC, IQ_POINT_COUNT, IQ_VAB, IQ_VBC, IQ_VCA,
    IQ_VAN, IQ_VBN, IQ_VCN, IQ_P_W, IQ_DEMAND_W, IQ_ENERGY_Wh,
    IQ_FREQUENCY_Hz, IQ_Q_var, IQ_PF
};
static const iq_key_t trip_keys[16] = {
    IQ_TRIP_IA, IQ_TRIP_IB, IQ_TRIP_IC, IQ_POINT_COUNT,
    IQ_TRIP_VAB, IQ_TRIP_VBC, IQ_TRIP_VCA, IQ_TRIP_VAN, IQ_TRIP_VBN,
    IQ_TRIP_VCN, IQ_TRIP_P_W, IQ_TRIP_DEMAND_W, IQ_TRIP_ENERGY_Wh,
    IQ_TRIP_FREQUENCY_Hz, IQ_TRIP_Q_var, IQ_TRIP_PF
};

static bool analog(iq_value_t *out, iq_key_t key, uint32_t word, bool qualified)
{
    if ((unsigned)key == IQ_POINT_COUNT) return true; /* Reserved fourth current. */
    double value = 0;
    bool valid = iq_impacc(word >> 1, &value);
    put(out, key, value, valid && qualified);
    return valid;
}

static bool flags(iq_value_t *out, uint32_t payload, bool trip)
{
    static const iq_key_t keys[2][9] = {
        {IQ_FLAGS_TRIP, IQ_FLAGS_ALARM, IQ_FLAGS_EXTERNAL, IQ_FLAGS_OVERVOLTAGE,
         IQ_FLAGS_UNDERVOLTAGE, IQ_FLAGS_PHASE_UNBALANCE, IQ_FLAGS_PHASE_LOSS,
         IQ_FLAGS_PHASE_REVERSAL, IQ_FLAGS_RAM_ROM_FAILURE},
        {IQ_TRIP_TRIP, IQ_TRIP_ALARM, IQ_TRIP_EXTERNAL, IQ_TRIP_OVERVOLTAGE,
         IQ_TRIP_UNDERVOLTAGE, IQ_TRIP_PHASE_UNBALANCE, IQ_TRIP_PHASE_LOSS,
         IQ_TRIP_PHASE_REVERSAL, IQ_TRIP_RAM_ROM_FAILURE}
    };
    unsigned t = payload & 255, a = (payload >> 8) & 255;
    put(out, keys[trip][0], t, t <= 1);
    put(out, keys[trip][1], a, a <= 1);
    for (unsigned i = 0; i < 7; ++i)
        put(out, keys[trip][2+i], (payload >> (16+i)) & 1, true);
    return t == 1 && a <= 1;
}

static void settings(iq_value_t *out, const uint32_t *words)
{
    static const double ct[] = {100,150,200,250,300,400,500,600,800,1000,1200,
        1500,1600,2000,2500,3000,3200,4000,5000};
    static const double pt[] = {1,2,4,5,20,30,35,40,55,60,70,100,120,30,60,100};
    static const double volts[8][2] = {{120,69},{208,120},{220,127},{240,138},
        {380,219},{416,240},{460,266},{575,332}};
    static const double pulse[] = {100,500,1000,5000,10000,50000,100000,500000,
        1000000,5000000,10000000,50000000,100000000,500000000,500000000,500000000};
    const unsigned header = words[0] >> 1;
    put(out, IQ_FIRMWARE_REVISION, (header >> 8) & 255, true);
    put(out, IQ_FIRMWARE_VERSION, header >> 16, true);
    unsigned sw[6];
    for (unsigned b = 0; b < 6; ++b) {
        sw[b] = (words[1+b/3] >> (1 + 8*(b%3))) & 255;
        put(out, IQ_SW1_RAW+b, sw[b], true);
        for (unsigned bit = 0; bit < 8; ++bit)
            put(out, IQ_SW1_1_ON+b*8+bit, !(sw[b] & (1u<<bit)), true);
    }
    unsigned a=sw[0], b=sw[1], c=sw[2], d=sw[3], e=sw[4], f=sw[5];
    unsigned ct_code=a&31, pt_code=c&15;
    bool international=pt_code>=13;
    const double minutes[] = {5,10,15,30};
    const double values[] = {ct_code<19?ct[ct_code]:NAN, 5, a&32?50:60,
        a&128?3:4, pt[pt_code], minutes[(c>>4)&3],
        international?110:volts[d&7][0], international?64:volts[d&7][1],
        pulse[d>>4], 105+5*(e&7), 95-5*(e>>5), 5+5*(f&7), 1+((f>>4)&7)};
    for (unsigned i=0; i<13; ++i) put(out, IQ_CONFIG_CT_PRIMARY_A+i, values[i], true);
    const bool binary[] = {!(a&64), !(a&128), !(c&64), !(c&128), !(d&8),
        !!(e&8), !(e&16), !(f&8), !!(b&1), !!(b&2), !!(b&4), !!(b&8),
        !!(b&16), !!(b&32), !!(b&64), !!(b&128), international};
    for (unsigned i=0; i<17; ++i) put(out, IQ_CONFIG_DELAY_ENABLED+i, binary[i], true);
}

bool iq_decode(iq_kind_t kind, const uint32_t *words, size_t count,
               iq_value_t out[IQ_POINT_COUNT])
{
    if (!out) return false;
    memset(out, 0, sizeof(iq_value_t)*IQ_POINT_COUNT);
    if (!words || kind >= IQ_KIND_COUNT || !count || count > 18) return false;
    for (size_t i=0; i<count; ++i)
        if (words[i]>0x1ffffff || (words[i]&1)) return false;
    const uint32_t header=words[0]>>1;
    if (kind == IQ_STANDARD) {
        static const unsigned counts[] = {4,3,3,3,3,1};
        if (header & ~0x3f07ffu) return false;
        size_t expected=1;
        for (unsigned b=0; b<6; ++b) if (header & (1u<<(b+5))) expected+=counts[b];
        if (expected != count) return false;
        unsigned word=1, slot=0;
        for (unsigned b=0; b<6; ++b) {
            bool supported=header & (1u<<(b+5));
            for (unsigned i=0; i<counts[b]; ++i, ++slot) {
                if (!supported) continue;
                if (b == 5) put(out, IQ_ENERGY_kWh, words[word]>>1, true);
                else analog(out, live_keys[slot], words[word], true);
                ++word;
            }
        }
        if (out[IQ_P_W].valid && out[IQ_Q_var].valid) {
            double apparent=hypot(out[IQ_P_W].value,out[IQ_Q_var].value);
            put(out,IQ_S_PQ_estimate_VA,apparent,true);
            put(out,IQ_PF_PQ_magnitude,apparent?fabs(out[IQ_P_W].value)/apparent:0,apparent>0);
        }
    } else {
        unsigned expected=kind==IQ_FLAGS?2:kind==IQ_SETTINGS?3:18;
        if (count!=expected || (header&255)!=expected-1) return false;
        if (kind==IQ_SETTINGS) settings(out,words);
        else {
            bool evidence=flags(out,words[1]>>1,kind==IQ_TRIP);
            if (kind==IQ_TRIP) {
                bool valid=true;
                for (unsigned i=0;i<16;++i)
                    valid &= analog(out,trip_keys[i],words[i+2],evidence);
                put(out,IQ_TRIP_DATA_VALID,evidence&&valid,
                    out[IQ_TRIP_TRIP].valid&&out[IQ_TRIP_ALARM].valid);
            }
        }
    }
    return true;
}
