/* OS/hardware adapters for the complete production action/keymap path. */
static int activity=ACTIVITY_MAINMENU;
static int root_result;
static struct {int raw,held,ticks;} raw_events[16];
static int raw_first,raw_count;
static void enqueue_raw(int raw,int held,int ticks){CHECK(raw_count<16);int at=(raw_first+raw_count++)%16;raw_events[at].raw=raw;raw_events[at].held=held;raw_events[at].ticks=ticks;}
static int take_raw(void){int at=raw_first;raw_first=(raw_first+1)%16;raw_count--;raw_button=raw_events[at].raw;held_button=raw_events[at].held;bridge_advance(raw_events[at].ticks);return raw_button;}
long button_get_w_tmo(int timeout){
 if(raw_count)return take_raw();
 if(!timeout)return BUTTON_NONE;
 int a=bridge_action(&raw_button,&held_button);
 if(a==ACTION_NONE)return BUTTON_NONE;
 if(a==ACTION_STD_MENU){enqueue_raw(BUTTON_RC_MENU,BUTTON_RC_MENU,0);enqueue_raw(BUTTON_RC_MENU|BUTTON_REL,0,1);}
 else if(a==ACTION_STD_OK&&raw_button==BUTTON_RIGHT)enqueue_raw(BUTTON_RC_RIGHT,0,0);
 else if(a==ACTION_STD_CANCEL)enqueue_raw(BUTTON_RC_LEFT,0,0);
 else if(a==ACTION_STD_OK){enqueue_raw(BUTTON_SELECT,BUTTON_SELECT,0);enqueue_raw(BUTTON_SELECT|BUTTON_REL,0,1);}
 else if(a==ACTION_AGENTS_REMOTE_TALK)enqueue_raw(BUTTON_RC_PLAY,BUTTON_RC_PLAY,0);
 else if(a==ACTION_AGENTS_TALK_END)enqueue_raw(BUTTON_RC_PLAY|BUTTON_REL,0,0);
 else CHECK(0);
 return take_raw();
}
void button_clear_queue(void){raw_first=raw_count=0;}
void button_clear_pressed(void){}
intptr_t button_get_data(void){return 0;}
void button_set_ui_filter(long(*f)(long)){(void)f;}
void button_set_ui_timeout(int(*f)(int)){(void)f;}
unsigned int thread_self(void){return 1;}
void podlink_time_sync(void){}
void cider_source_poll(void){}
void cider_play_status(void){}
bool cider_active(void){return false;}
void cider_remote_source(void){}
void iap_volume_context(bool w){(void)w;}
int iap_agents_volume_take(void){return 0;}
int iap_volume_filter(int b){return b;}
bool remote_input_active;
enum current_activity get_current_activity(void){return activity;}
void backlight_on_ignore(bool b,int t){(void)b;(void)t;}
void send_event(unsigned short id,void*d){(void)id;(void)d;}
void keyclick_click(bool b,int a){(void)b;(void)a;}
bool power_input_present(void){return true;}
void root_menu_agents_shortcut(void){activity=ACTIVITY_AGENTS;root_result=agents_screen();activity=ACTIVITY_MAINMENU;}
void root_menu_wps_shortcut(void){CHECK(0);}
void root_menu_cover_shortcut(void){CHECK(0);}
void *core_get_data(int h){(void)h;return NULL;}
int full_action_home(void){
 CHECK(!raw_count);root_result=-1;
 enqueue_raw(BUTTON_RC_PLAY,BUTTON_RC_PLAY,1);enqueue_raw(BUTTON_RC_PLAY|BUTTON_REL,0,13);
 enqueue_raw(BUTTON_RC_PLAY,BUTTON_RC_PLAY,15);enqueue_raw(BUTTON_RC_PLAY|BUTTON_REL,0,13);
 enqueue_raw(0,0,21);
 for(int i=0;i<12&&root_result<0;i++)get_action(CONTEXT_MAINMENU,10);
 CHECK(root_result==AGENTS_EXIT_MENU);return root_result;
}
