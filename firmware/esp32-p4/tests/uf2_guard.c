#include "iq_uf2.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static void word(uint8_t *p,uint32_t n)
{ for(unsigned i=0;i<4;++i)p[i]=(uint8_t)(n>>(8*i)); }
int main(int argc,char **argv)
{
    uint8_t b[1536]={0};char error[192];size_t skip;
    for(unsigned i=0;i<3;++i) {
        uint8_t *p=b+i*512;
        word(p,0x0a324655);word(p+4,0x9e5d5157);word(p+8,0x2000);word(p+12,0x10000000+i*256);
        word(p+16,256);word(p+20,i);word(p+24,3);word(p+28,0xe48bff59);word(p+508,0x0ab16f30);
    }
    static const char metadata[]="iqdata-pico-live\0000.4.7\000pico2";
    memcpy(b+32,metadata,sizeof(metadata));
    assert(iq_uf2_validate(b,sizeof(b),&skip,error,sizeof(error))&&skip==0);
    for(unsigned offset=0;offset<32;offset+=4) {
        b[512+offset]^=1;assert(!iq_uf2_validate(b,sizeof(b),&skip,error,sizeof(error)));b[512+offset]^=1;
    }
    b[1020]^=1;assert(!iq_uf2_validate(b,sizeof(b),&skip,error,sizeof(error)));b[1020]^=1;
    assert(!iq_uf2_validate(b,sizeof(b)-1,&skip,error,sizeof(error)));
    b[32]='X';assert(!iq_uf2_validate(b,sizeof(b),&skip,error,sizeof(error)));
    if(argc==2) {
        FILE *f=fopen(argv[1],"rb");assert(f);assert(!fseek(f,0,SEEK_END));long n=ftell(f);rewind(f);
        assert(n>0&&n<=IQ_UF2_MAX_BYTES);uint8_t *data=malloc(n);assert(data&&fread(data,1,n,f)==(size_t)n);fclose(f);
        bool ok=iq_uf2_validate(data,n,&skip,error,sizeof(error));
        if(!ok)fprintf(stderr,"%s\n",error);assert(ok);free(data);
    }
    puts("UF2 validation and rejection checks passed");return 0;
}
