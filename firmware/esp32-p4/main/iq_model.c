/* Bounded RAM port of live/live_diagnostics.py. No measurement persistence. */
#include "iq_model.h"
#include <math.h>
#include <stdlib.h>
#include <string.h>

#define POWER_CAPACITY 1024u
#define ENERGY_CAPACITY 85000u
typedef struct { double a,b,p0,p1; } power_interval_t;
typedef struct { double time,value; } energy_sample_t;
struct iq_model {
    iq_value_t raw[IQ_POINT_COUNT];
    bool group_good[IQ_KIND_COUNT], group_attempted[IQ_KIND_COUNT], have_previous, broken;
    uint64_t last_standard, last_rejected;
    double previous_time, previous_power, previous_energy, first_time;
    double nominal_v, nominal_hz, previous_flags[3], durations[3][2];
    double extremes[5][2], integral, power_seconds, energy_total, energy_seconds;
    double excluded, samples, gaps, resets, out_of_order;
    power_interval_t power[POWER_CAPACITY];
    energy_sample_t energy[ENERGY_CAPACITY];
    unsigned power_start,power_count,energy_start,energy_count;
    uint64_t attempts[2],failures[2],consecutive;
    uint64_t malformed;
};

size_t iq_model_bytes(void) { return sizeof(iq_model_t); }

iq_quality_t iq_value_quality(const iq_value_t *v)
{
    if(v->valid) return isfinite(v->value)?IQ_QUALITY_GOOD:IQ_QUALITY_METER_VALUE;
    return v->quality>IQ_QUALITY_GOOD&&v->quality<IQ_QUALITY_COUNT?v->quality:IQ_QUALITY_NO_DATA;
}
const char *iq_quality_name(iq_quality_t q)
{
    static const char *names[]={"good","not-received","communication-failure","stale",
        "invalid-meter-value","undefined-zero-load","insufficient-history","settings-unavailable",
        "no-trip-snapshot","unsupported-by-meter","undefined-calculation"};
    return (unsigned)q<IQ_QUALITY_COUNT?names[q]:"unknown";
}
const char *iq_quality_description(iq_quality_t q)
{
    static const char *descriptions[]={"Valid","No reading received","Meter communication failed",
        "Reading is stale","Meter value cannot support this calculation","Undefined at zero load",
        "Collecting continuous history","Nominal meter settings unavailable","No qualified trip snapshot",
        "Meter does not supply this value","Calculation is undefined for these inputs"};
    return (unsigned)q<IQ_QUALITY_COUNT?descriptions[q]:"Unknown quality";
}
unsigned iq_quality_reliability(iq_quality_t q)
{
    if(q==IQ_QUALITY_GOOD)return 0;
    if(q==IQ_QUALITY_NO_DATA||q==IQ_QUALITY_COMMUNICATION||q==IQ_QUALITY_STALE)return 12;
    return q==IQ_QUALITY_METER_VALUE?8:7;
}
const char *iq_quality_reliability_name(iq_quality_t q)
{
    switch(iq_quality_reliability(q)) {
    case 0:return "no-fault-detected";
    case 12:return "communication-failure";
    case 8:return "process-error";
    default:return "unreliable-other";
    }
}

iq_model_t *iq_model_create(void)
{
    iq_model_t *m=calloc(1,sizeof(*m));
    if (!m) return NULL;
    m->nominal_v=m->nominal_hz=NAN;
    m->previous_power=m->previous_energy=NAN;
    for (unsigned i=0;i<3;++i) m->previous_flags[i]=NAN;
    for (unsigned i=0;i<5;++i) { m->extremes[i][0]=INFINITY; m->extremes[i][1]=-INFINITY; }
    return m;
}
void iq_model_destroy(iq_model_t *m) { free(m); }

static void put(iq_model_t *m, iq_key_t key, double value, uint64_t now)
{
    iq_value_t *v=&m->raw[key];
    v->valid=isfinite(value);
    v->quality=v->valid?IQ_QUALITY_GOOD:IQ_QUALITY_HISTORY;
    if (v->valid) { v->value=value; v->stamp=now; }
}

static bool fresh(const iq_value_t *v, uint64_t now, double seconds)
{
    return v->valid && now>=v->stamp && (!seconds || (now-v->stamp)<=seconds*1000);
}

static double value(iq_model_t *m, iq_key_t key)
{
    return m->raw[key].valid ? m->raw[key].value : NAN;
}

static void nominals(iq_model_t *m, uint64_t now)
{
    const iq_key_t source[]={IQ_CONFIG_NOMINAL_LL_V,IQ_CONFIG_FREQUENCY_Hz};
    const iq_key_t depend[2][4]={
        {IQ_NOMINAL_VOLTAGE_V,IQ_VOLTAGE_EXCURSION_ACTIVE,IQ_VOLTAGE_EXCURSION_CONTINUOUS_s,IQ_VOLTAGE_EXCURSION_TOTAL_s},
        {IQ_NOMINAL_FREQUENCY_Hz,IQ_FREQUENCY_EXCURSION_ACTIVE,IQ_FREQUENCY_EXCURSION_CONTINUOUS_s,IQ_FREQUENCY_EXCURSION_TOTAL_s}};
    double *stored[]={&m->nominal_v,&m->nominal_hz};
    for(unsigned i=0;i<2;++i) {
        iq_value_t *v=&m->raw[source[i]];
        double next=m->group_good[IQ_SETTINGS]&&fresh(v,now,3600)&&v->value>0?v->value:NAN;
        if (next==*stored[i] || (isnan(next)&&isnan(*stored[i]))) continue;
        *stored[i]=next;
        unsigned flag=i==0?2:1;
        m->durations[flag][0]=0;
        m->previous_flags[flag]=NAN;
        for(unsigned k=0;k<4;++k) {
            m->raw[depend[i][k]].valid=false;
            m->raw[depend[i][k]].quality=IQ_QUALITY_SETTINGS;
        }
    }
}

static void extreme(iq_model_t *m,unsigned index,double v)
{
    if (!isfinite(v)) return;
    if(v<m->extremes[index][0]) m->extremes[index][0]=v;
    if(v>m->extremes[index][1]) m->extremes[index][1]=v;
}

static energy_sample_t *energy_at(iq_model_t *m,unsigned index)
{
    return &m->energy[(m->energy_start+index)%ENERGY_CAPACITY];
}

static double energy_window(iq_model_t *m,double now,double seconds)
{
    double start=now-seconds;
    if(m->energy_count<2 || energy_at(m,0)->time>start ||
       energy_at(m,m->energy_count-1)->time<now) return NAN;
    /* Binary search avoids scanning a day of data every sample. */
    unsigned lo=0,hi=m->energy_count-1;
    while(lo<hi) { unsigned mid=lo+(hi-lo)/2;
        if(energy_at(m,mid)->time<start) lo=mid+1; else hi=mid; }
    energy_sample_t *b=energy_at(m,lo);
    double boundary=b->value;
    if(b->time!=start) {
        if(!lo) return NAN;
        energy_sample_t *a=energy_at(m,lo-1);
        boundary=a->value+(b->value-a->value)*(start-a->time)/(b->time-a->time);
    }
    return energy_at(m,m->energy_count-1)->value-boundary;
}

static double imbalance(const double v[3])
{
    double mean=(v[0]+v[1]+v[2])/3;
    if(!isfinite(mean)||mean<=0) return NAN;
    return 100*fmax(fabs(v[0]-mean),fmax(fabs(v[1]-mean),fabs(v[2]-mean)))/mean;
}

static void unavailable(iq_model_t *m,iq_key_t key,iq_quality_t reason)
{ if(!m->raw[key].valid)m->raw[key].quality=reason; }

static void derive(iq_model_t *m,uint64_t stamp)
{
    double now=stamp/1000.0;
    if(m->have_previous && now<=m->previous_time) {
        if(now<m->previous_time) {
            if(stamp!=m->last_rejected) { ++m->out_of_order; m->last_rejected=stamp; }
            m->broken=true;
            for(unsigned i=0;i<IQ_POINT_COUNT;++i)
                if(iq_points[i].group==IQ_GROUP_DERIVED) m->raw[i].valid=false;
        }
        return;
    }
    double currents[]={value(m,IQ_IA),value(m,IQ_IB),value(m,IQ_IC)};
    double volts[]={value(m,IQ_VAB),value(m,IQ_VBC),value(m,IQ_VCA)};
    for(unsigned i=0;i<3;++i) {
        if(currents[i]<0) currents[i]=NAN;
        if(volts[i]<0) volts[i]=NAN;
        extreme(m,0,volts[i]); extreme(m,1,currents[i]);
    }
    double power=value(m,IQ_P_W),freq=value(m,IQ_FREQUENCY_Hz);
    double pf=fabs(value(m,IQ_PF)),energy=value(m,IQ_ENERGY_kWh);
    if(freq<=0) freq=NAN;
    if(pf>1) pf=NAN;
    if(energy<0||energy>16777215) energy=NAN;
    extreme(m,2,freq); extreme(m,3,pf); extreme(m,4,power);
    double flags[]={isfinite(pf)?pf<.9:NAN,
        isfinite(freq)&&isfinite(m->nominal_hz)?fabs(freq-m->nominal_hz)>.5:NAN,NAN};
    if(isfinite(m->nominal_v)&&isfinite(volts[0]+volts[1]+volts[2])) {
        flags[2]=0;
        for(unsigned i=0;i<3;++i) if(fabs(volts[i]-m->nominal_v)>m->nominal_v*.1) flags[2]=1;
    }
    double dt=m->have_previous?now-m->previous_time:NAN;
    bool consecutive=isfinite(dt)&&dt<=5&&!m->broken;
    if(isfinite(dt)&&dt>5) { ++m->gaps; m->excluded+=dt; }
    bool good_power=consecutive&&isfinite(power)&&isfinite(m->previous_power);
    if(good_power) {
        if(m->power_count==POWER_CAPACITY) { m->power_start=(m->power_start+1)%POWER_CAPACITY; --m->power_count; }
        m->power[(m->power_start+m->power_count++)%POWER_CAPACITY]=
            (power_interval_t){m->previous_time,now,m->previous_power,power};
        m->integral+=(m->previous_power+power)*.5*dt; m->power_seconds+=dt;
    }
    for(unsigned i=0;i<3;++i) {
        if(consecutive&&flags[i]==1&&m->previous_flags[i]==1) {
            m->durations[i][0]+=dt; m->durations[i][1]+=dt;
        } else m->durations[i][0]=0;
    }
    bool decrease=isfinite(energy)&&isfinite(m->previous_energy)&&energy<m->previous_energy;
    if(decrease) ++m->resets;
    if(consecutive&&isfinite(energy)&&isfinite(m->previous_energy)&&!decrease) {
        m->energy_total+=energy-m->previous_energy; m->energy_seconds+=dt;
    } else { m->energy_start=m->energy_count=0; }
    if(isfinite(energy)) {
        if(m->energy_count==ENERGY_CAPACITY) { m->energy_start=(m->energy_start+1)%ENERGY_CAPACITY; --m->energy_count; }
        *energy_at(m,m->energy_count++)=(energy_sample_t){now,energy};
        while(m->energy_count>2&&energy_at(m,1)->time<=now-86400) {
            m->energy_start=(m->energy_start+1)%ENERGY_CAPACITY; --m->energy_count;
        }
    }
    while(m->power_count&&m->power[m->power_start].b<=now-900) {
        m->power_start=(m->power_start+1)%POWER_CAPACITY; --m->power_count;
    }
    double integral=0,coverage=0,peak=-INFINITY,lowest=INFINITY;
    for(unsigned i=0;i<m->power_count;++i) {
        const power_interval_t *p=&m->power[(m->power_start+i)%POWER_CAPACITY];
        double lo=fmax(p->a,now-900),hi=fmin(p->b,now);
        if(hi<=lo) continue;
        double q0=p->p0+(p->p1-p->p0)*(lo-p->a)/(p->b-p->a);
        double q1=p->p0+(p->p1-p->p0)*(hi-p->a)/(p->b-p->a);
        integral+=(q0+q1)*.5*(hi-lo); coverage+=hi-lo;
        peak=fmax(peak,fmax(q0,q1)); lowest=fmin(lowest,fmin(q0,q1));
    }
    bool complete=fabs(coverage-900)<1e-5;
    double hour=energy_window(m,now,3600),day=energy_window(m,now,86400);
    if(!m->samples) m->first_time=now;
    ++m->samples;
    double highest=NAN,phase=NAN;
    if(isfinite(currents[0]+currents[1]+currents[2])) {
        highest=currents[0]; phase=1;
        for(unsigned i=1;i<3;++i) if(currents[i]>highest) { highest=currents[i]; phase=i+1; }
        if(highest<=0) phase=NAN;
    }
    const double values[]={
        imbalance(volts),imbalance(currents),phase,highest,
        m->extremes[0][0],m->extremes[0][1],m->extremes[1][0],m->extremes[1][1],
        m->extremes[2][0],m->extremes[2][1],m->extremes[3][0],m->extremes[3][1],
        m->power_seconds?m->integral/m->power_seconds/1000:NAN,m->extremes[4][1]/1000,
        good_power?(power-m->previous_power)/1000:NAN,
        isfinite(flags[0])?m->durations[0][0]:NAN,isfinite(flags[0])?m->durations[0][1]:NAN,
        isfinite(flags[1])?m->durations[1][0]:NAN,isfinite(flags[1])?m->durations[1][1]:NAN,
        isfinite(flags[2])?m->durations[2][0]:NAN,isfinite(flags[2])?m->durations[2][1]:NAN,
        complete?integral/900/1000:NAN,complete&&peak>0&&lowest>=0?100*integral/900/peak:NAN,
        m->power_seconds/3600,coverage,m->energy_seconds>0?m->energy_total:NAN,hour,day,
        m->energy_count?(now-energy_at(m,0)->time)/3600:NAN,m->samples,now-m->first_time,
        m->gaps,m->resets,m->out_of_order,m->excluded,m->nominal_v,m->nominal_hz,.9,10,.5,
        m->energy_seconds/3600,dt};
    _Static_assert(sizeof(values)/sizeof(values[0])==42,"Derived analog catalog");
    for(unsigned i=0;i<42;++i) put(m,IQ_VLL_IMBALANCE_pct+i,values[i],stamp);
    for(unsigned i=0;i<3;++i) put(m,IQ_LOW_PF_ACTIVE+i,flags[i],stamp);
    put(m,IQ_ROLLING_DEMAND_READY,complete,stamp);
    put(m,IQ_ROLLING_HOUR_ENERGY_READY,isfinite(hour),stamp);
    put(m,IQ_ROLLING_DAY_ENERGY_READY,isfinite(day),stamp);
    iq_quality_t current_reason=!isfinite(currents[0]+currents[1]+currents[2])?
        IQ_QUALITY_METER_VALUE:IQ_QUALITY_ZERO_LOAD;
    unavailable(m,IQ_CURRENT_IMBALANCE_pct,current_reason);
    unavailable(m,IQ_HIGHEST_CURRENT_PHASE,current_reason);
    unavailable(m,IQ_HIGHEST_CURRENT_A,IQ_QUALITY_METER_VALUE);
    unavailable(m,IQ_VLL_IMBALANCE_pct,isfinite(volts[0]+volts[1]+volts[2])?
        IQ_QUALITY_UNDEFINED:IQ_QUALITY_METER_VALUE);
    const iq_key_t frequency_keys[]={IQ_SESSION_FREQUENCY_MIN_Hz,IQ_SESSION_FREQUENCY_MAX_Hz,
        IQ_FREQUENCY_EXCURSION_ACTIVE,IQ_FREQUENCY_EXCURSION_CONTINUOUS_s,IQ_FREQUENCY_EXCURSION_TOTAL_s};
    for(unsigned i=0;i<sizeof(frequency_keys)/sizeof(frequency_keys[0]);i++)
        unavailable(m,frequency_keys[i],!isfinite(freq)?IQ_QUALITY_METER_VALUE:IQ_QUALITY_SETTINGS);
    const iq_key_t nominal_keys[]={IQ_NOMINAL_VOLTAGE_V,IQ_NOMINAL_FREQUENCY_Hz,
        IQ_VOLTAGE_EXCURSION_ACTIVE,IQ_VOLTAGE_EXCURSION_CONTINUOUS_s,IQ_VOLTAGE_EXCURSION_TOTAL_s};
    for(unsigned i=0;i<sizeof(nominal_keys)/sizeof(nominal_keys[0]);i++)
        unavailable(m,nominal_keys[i],IQ_QUALITY_SETTINGS);
    const iq_key_t pf_keys[]={IQ_SESSION_PF_MIN,IQ_SESSION_PF_MAX,IQ_LOW_PF_ACTIVE,
        IQ_LOW_PF_CONTINUOUS_s,IQ_LOW_PF_TOTAL_s};
    for(unsigned i=0;i<sizeof(pf_keys)/sizeof(pf_keys[0]);i++)
        unavailable(m,pf_keys[i],IQ_QUALITY_METER_VALUE);
    if(complete)unavailable(m,IQ_ROLLING_15MIN_LOAD_FACTOR_pct,
        peak==0&&lowest==0?IQ_QUALITY_ZERO_LOAD:IQ_QUALITY_UNDEFINED);
    m->previous_time=now; m->previous_power=power; m->previous_energy=energy;
    memcpy(m->previous_flags,flags,sizeof(flags)); m->have_previous=true; m->broken=false;
}

static iq_group_t group_for(iq_kind_t kind)
{
    const iq_group_t groups[]={IQ_GROUP_LIVE,IQ_GROUP_FLAGS,IQ_GROUP_SETTINGS,IQ_GROUP_TRIP};
    return groups[kind];
}

static void health(iq_model_t *m,iq_kind_t kind,uint64_t now,double duration,int stop,uint32_t malformed,bool good)
{
    unsigned group=kind==IQ_STANDARD?0:1;
    m->group_attempted[kind]=true;
    ++m->attempts[group]; m->failures[group]+=!good;
    if(!group) {
        m->consecutive=good?0:m->consecutive+1; m->malformed+=malformed;
        put(m,IQ_POLL_ATTEMPTS,m->attempts[0],now); put(m,IQ_POLL_FAILURES,m->failures[0],now);
        put(m,IQ_POLL_FAILURE_PERCENT,100.0*m->failures[0]/m->attempts[0],now);
        put(m,IQ_POLL_MALFORMED,m->malformed,now); put(m,IQ_POLL_DURATION,duration,now);
        put(m,IQ_POLL_CONSECUTIVE_FAILURES,m->consecutive,now);
        put(m,IQ_POLL_LAST_STOP,stop>=0?stop:NAN,now);
    } else { put(m,IQ_DIAG_ATTEMPTS,m->attempts[1],now); put(m,IQ_DIAG_FAILURES,m->failures[1],now); }
    for(unsigned i=0;i<IQ_POINT_COUNT;++i)
        if(iq_points[i].group==IQ_GROUP_HEALTH&&m->raw[i].valid) m->raw[i].stamp=now;
}

void iq_model_fail(iq_model_t *m,iq_kind_t kind,uint64_t now,double duration,int stop,uint32_t malformed)
{
    if(!m||kind>=IQ_KIND_COUNT) return;
    m->group_good[kind]=false;
    if(kind==IQ_STANDARD) m->broken=true;
    nominals(m,now);
    health(m,kind,now,duration,stop,malformed,false);
}

bool iq_model_accept(iq_model_t *m,iq_kind_t kind,const uint32_t *words,size_t count,
                     uint64_t now,double duration,int stop,uint32_t malformed)
{
    if(!m||kind>=IQ_KIND_COUNT) return false;
    iq_value_t decoded[IQ_POINT_COUNT];
    if(!iq_decode(kind,words,count,decoded)||stop!=0||malformed) {
        iq_model_fail(m,kind,now,duration,stop,malformed); return false;
    }
    iq_group_t group=group_for(kind);
    for(unsigned i=0;i<IQ_POINT_COUNT;++i) if(iq_points[i].group==group) {
        if(decoded[i].valid) { m->raw[i]=decoded[i]; m->raw[i].stamp=now; }
        else {
            m->raw[i].valid=false;
            m->raw[i].quality=decoded[i].quality==IQ_QUALITY_GOOD?
                IQ_QUALITY_UNSUPPORTED:decoded[i].quality;
        }
    }
    m->group_good[kind]=true;
    nominals(m,now);
    if(kind==IQ_STANDARD) { m->last_standard=now; derive(m,now); }
    health(m,kind,now,duration,stop,malformed,true);
    return true;
}

void iq_model_snapshot(iq_model_t *m,uint64_t now,iq_value_t out[IQ_POINT_COUNT])
{
    if(!m||!out) return;
    nominals(m,now);
    bool live=m->group_good[IQ_STANDARD]&&now>=m->last_standard&&now-m->last_standard<=5000;
    if(!live) m->broken=true;
    memcpy(out,m->raw,sizeof(m->raw));
    bool all_good=true;
    for(unsigned i=0;i<IQ_POINT_COUNT;++i) {
        const iq_point_def_t *d=&iq_points[i];
        if(out[i].valid&&!fresh(&out[i],now,d->max_age)) {
            out[i].valid=false;out[i].quality=IQ_QUALITY_STALE;
        }
        int source=-1;
        if(d->group==IQ_GROUP_LIVE||d->group==IQ_GROUP_DERIVED)source=IQ_STANDARD;
        for(unsigned kind=IQ_FLAGS;kind<IQ_KIND_COUNT;++kind)
            if(d->group==group_for(kind))source=(int)kind;
        if(source>=0&&!m->group_good[source]) {
            out[i].valid=false;
            out[i].quality=m->group_attempted[source]?IQ_QUALITY_COMMUNICATION:IQ_QUALITY_NO_DATA;
        } else if(source==IQ_STANDARD&&!live) {
            out[i].valid=false;out[i].quality=IQ_QUALITY_STALE;
        }
        out[i].quality=iq_value_quality(&out[i]);
        if(d->group==IQ_GROUP_LIVE) all_good &= out[i].valid;
        out[i].value*=d->scale;
    }
    out[IQ_age_seconds]=(iq_value_t){.value=now>=m->last_standard?(now-m->last_standard)/1000.0:0,
        .stamp=now,.valid=m->samples>0&&now>=m->last_standard};
    out[IQ_all_18_live_readings_good]=(iq_value_t){.value=all_good,.stamp=now,.valid=true};
    out[IQ_display_verified]=(iq_value_t){.value=0,.stamp=now,.valid=true};
}
