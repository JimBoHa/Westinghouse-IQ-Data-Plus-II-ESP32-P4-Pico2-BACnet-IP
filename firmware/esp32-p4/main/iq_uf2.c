/* Strict validator for the separately built plain Pico 2 application. */
#include "iq_uf2.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static uint32_t u32(const uint8_t *p)
{ return p[0]|((uint32_t)p[1]<<8)|((uint32_t)p[2]<<16)|((uint32_t)p[3]<<24); }
static bool bad(char *error,size_t size,const char *message)
{ snprintf(error,size,"%s",message);return false; }
static bool contains(const uint8_t *data,size_t size,const char *text)
{
    size_t n=strlen(text)+1;
    for(size_t i=0;i+n<=size;++i)if(!memcmp(data+i,text,n))return true;
    return false;
}
bool iq_uf2_validate_version(const uint8_t *data,size_t length,size_t *skip,
    char version[16],char *error,size_t size)
{
    if(!data||length<1024||length>IQ_UF2_MAX_BYTES||length%512)
        return bad(error,size,"UF2 must contain complete 512-byte blocks, up to 1 MiB");
    size_t offset=0;
    /* picotool prepends an absolute-family compatibility block outside the
     * physical 4 MiB flash. Validate its header, then omit it on this RP2350. */
    if(u32(data+8)==0xa000&&u32(data+12)==0x10ffff00&&u32(data+16)==256&&
       u32(data+20)==0&&u32(data+24)==2&&u32(data+28)==0xe48bff57)offset=512;
    for(size_t i=0;i<length;i+=512)
        if(u32(data+i)!=0x0a324655||u32(data+i+4)!=0x9e5d5157||u32(data+i+508)!=0x0ab16f30)
            return bad(error,size,"Bad UF2 magic or truncated block");
    size_t blocks=(length-offset)/512;
    uint8_t *image=malloc(blocks*256);
    if(!image)return bad(error,size,"No memory for UF2 validation");
    bool ok=true;
    for(size_t i=0;i<blocks;++i) {
        const uint8_t *b=data+offset+i*512;
        if(u32(b+8)!=0x2000||u32(b+12)!=0x10000000u+i*256||u32(b+16)!=256||
           u32(b+20)!=i||u32(b+24)!=blocks||u32(b+28)!=0xe48bff59) { ok=false;break; }
        memcpy(image+i*256,b+32,256);
    }
    bool old=ok&&contains(image,blocks*256,"0.4.7"),current=ok&&contains(image,blocks*256,"0.4.8");
    if(ok)ok=contains(image,blocks*256,"iqdata-pico-live")&&(old!=current)&&contains(image,blocks*256,"pico2");
    if(ok&&version)snprintf(version,16,"%s",current?"0.4.8":"0.4.7");
    free(image);
    if(!ok)return bad(error,size,"Require complete contiguous iqdata-pico-live 0.4.7/0.4.8, plain pico2, RP2350 ARM Secure UF2");
    *skip=offset;error[0]=0;return true;
}
bool iq_uf2_validate(const uint8_t *data,size_t length,size_t *skip,char *error,size_t size)
{ return iq_uf2_validate_version(data,length,skip,NULL,error,size); }
