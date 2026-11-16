/* Real session/serial plus resident owner, deterministic worker, no USB device. */
#define PODLINK_SERVICE_TEST
#define DEFAULT_STACK_SIZE 4096
#ifndef MAX
#define MAX(a,b) ((a) > (b) ? (a) : (b))
#endif
#define main transport_harness_main
#include "../podlink_firmware_test.c"
#undef main
#undef podlink_open
#undef podlink_state
#undef podlink_max_message
#undef podlink_send
#undef podlink_close
#undef podlink_poll
#undef podlink_wait

static int disk_requests;
void usb_podlink_disk(void) { disk_requests++; }
static int clock_wakes;
void button_queue_post(long id, intptr_t data) { assert(id == 0x20000000 && !data); clock_wakes++; }
#include "podlink_rust_service.h"
#include "herdremote_model.h"

#include <stdlib.h>
extern int agents_screen(void);
extern int screen_test_home_double(void);
extern int screen_test_action(const char *);
extern int screen_test_button(const char *);
extern int screen_test_view(void),screen_test_rows(void),screen_test_workspaces(void);
static char command[2*PODLINK_FRAME_MAX+256];
static void die(const char*s){fprintf(stderr,"bridge error: %s\n",s);exit(3);}
void bridge_assert(int ok,const char*s){if(!ok)die(s);}
static void bridge_pump(void){for(int i=0;i<20;i++){service_step();controller();}}
static void flush_wire(void){bridge_pump();if(output_size){printf("WIRE ");for(int i=0;i<output_size;i++)printf("%02x",output[i]);puts("");clear_output();}fflush(stdout);}
void bridge_advance(int ticks){current_tick+=ticks;bridge_pump();}
static int read_command(void){if(!fgets(command,sizeof command,stdin))exit(0);return 1;}
static bool rx(void){if(strncmp(command,"RX ",3))return false;static unsigned char raw[PODLINK_FRAME_MAX];unsigned n=0;for(char*p=command+3;p[0]&&p[0]!='\n';p+=2){unsigned v;if(sscanf(p,"%2x",&v)!=1||n>=sizeof raw)die("invalid RX");raw[n++]=v;}input_bytes(raw,n);bridge_pump();flush_wire();puts("OK");fflush(stdout);return true;}
int bridge_action(int*raw,int*held){
 flush_wire();printf("WAIT %d %d %d\n",screen_test_view(),screen_test_rows(),screen_test_workspaces());fflush(stdout);
 while(read_command()){
  if(rx())continue;
  if(!strncmp(command,"PUMP",4)){bridge_pump();flush_wire();puts("OK");fflush(stdout);continue;}
  char action[80],button[20];int ticks,down;
  if(sscanf(command,"ACTION %79s %19s %d %d",action,button,&ticks,&down)==4){
   *raw=screen_test_button(button);*held=down?screen_test_button("PLAY"):0;bridge_advance(ticks);return screen_test_action(action);}
  die("expected ACTION/RX/PUMP");
 }return 0;
}
int main(void){
 usb_serial_init();podlink_init();lease=true;service_step();insert();bridge_pump();flush_wire();puts("READY");fflush(stdout);
 while(read_command()){
  if(rx())continue;
  if(!strncmp(command,"PUMP",4)){bridge_pump();flush_wire();puts("OK");fflush(stdout);continue;}
  if(!strncmp(command,"ENTER",5)){int result=screen_test_home_double();
#ifndef FU46_REAL_ACTION
result=agents_screen();
#endif
flush_wire();printf("EXIT %d\n",result);fflush(stdout);continue;}
  if(!strncmp(command,"QUIT",4))break;
  die("unknown command");
 }return 0;
}
