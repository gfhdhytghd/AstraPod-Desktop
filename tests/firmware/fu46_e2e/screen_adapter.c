/* Included after the unmodified agents_screen.c in the target preprocessing
 * environment. Display/list/font and OS input are deterministic adapters.
 * The entire production screen is compiled, including all state transitions. */
extern void bridge_assert(int, const char *);
#define CHECK(x) bridge_assert(!!(x), #x)
extern int bridge_action(int *raw, int *held);
extern void bridge_advance(int ticks);
static int raw_button, held_button;
static struct viewport test_vp = {.width=320,.height=210,.font=FONT_UI};
static struct viewport *test_current_vp=&test_vp;
static struct font test_font={.height=14};
static int metric(const unsigned char *s,int *w,int *h){int n=0;for(;*s;s++)if((*s&0xc0)!=0x80)n+=*s>=0xe0?14:7;if(w)*w=n;if(h)*h=14;return n;}
static void nothing(void){}
static void colour(unsigned c){(void)c;}
static void mode(int m){(void)m;}
static void rect(int x,int y,int w,int h){(void)x;(void)y;CHECK(w>=0&&h>=0);}
static void point(int x,int y){(void)x;(void)y;}
static void line(int a,int b,int c){(void)a;(void)b;(void)c;}
static void puts_at(int x,int y,const unsigned char*s){(void)x;(void)y;CHECK(s);(void)strlen((const char*)s);}
static void alpha(const unsigned char*s,int a,int b,int c,int d,int e,int f,int g){CHECK(s);(void)a;(void)b;(void)c;(void)d;(void)e;(void)f;(void)g;}
static struct viewport *setvp(struct viewport*v){struct viewport*old=test_current_vp;test_current_vp=v?v:&test_vp;return old;}
struct screen screens[NB_SCREENS]={{.lcdwidth=320,.lcdheight=240,.depth=16,.is_color=true,
 .set_viewport=setvp,.getstringsize=metric,.set_foreground=colour,.set_background=colour,.set_drawmode=mode,
 .clear_viewport=nothing,.update_viewport=nothing,.update_viewport_rect=rect,.drawrect=rect,.fillrect=rect,
 .drawpixel=point,.hline=line,.vline=line,.putsxy=puts_at,.alpha_bitmap_part=alpha}};
struct font *font_get(int f){(void)f;return &test_font;}
void font_lock(int f,bool b){(void)f;(void)b;}
int font_get_width(struct font*f,ucschar_t ch){(void)f;return ch>=0x3000?14:7;}
int font_get_ui_bold(void){return FONT_UI;}
int font_getstringsize(const unsigned char*s,int*w,int*h,int f){(void)f;return metric(s,w,h);}
void viewport_set_fullscreen(struct viewport*v,enum screen_type s){(void)s;*v=test_vp;v->height=240;}
void viewportmanager_theme_enable(enum screen_type s,bool b,struct viewport*v){(void)s;(void)b;(void)v;}
void viewportmanager_theme_undo(enum screen_type s,bool b){(void)s;(void)b;}
void sb_skin_update(enum screen_type s,bool b){(void)s;(void)b;}
void gui_synclist_init(struct gui_synclist*l,list_get_name cb,void*d,bool scroll,int size,struct viewport*p){(void)p;memset(l,0,sizeof *l);l->callback_get_item_name=cb;l->data=d;l->scroll_all=scroll;l->selected_size=size;l->parent[0]=&test_vp;}
void gui_synclist_set_nb_items(struct gui_synclist*l,int n){CHECK(n>=0&&n<=64);l->nb_items=n;}
void gui_synclist_set_title(struct gui_synclist*l,const char*t,enum themable_icons i){l->title=t;l->title_icon=i;}
void gui_synclist_set_row_callbacks(struct gui_synclist*l,list_get_kind*k,list_get_name*v,list_is_playing*p){l->callback_get_item_kind=k;l->callback_get_item_value=v;l->callback_item_is_playing=p;}
void gui_synclist_set_description_callback(struct gui_synclist*l,list_get_name*f){l->callback_get_item_description=f;}
void gui_synclist_set_voice_callback(struct gui_synclist*l,list_speak_item*f){l->callback_speak_item=f;}
void gui_synclist_set_status_callback(struct gui_synclist*l,list_draw_status*f){l->callback_draw_status=f;}
int gui_synclist_get_nb_lines(struct gui_synclist*l,enum screen_type s){(void)l;(void)s;return 10;}
int gui_synclist_get_sel_pos(struct gui_synclist*l){return l->selected_item;}
void gui_synclist_select_item(struct gui_synclist*l,int i){l->selected_item=MAX(0,MIN(i,l->nb_items-1));}
void gui_synclist_set_top_item(struct gui_synclist*l,int i){l->start_item[0]=MAX(0,i);}
void gui_synclist_inhibit_flush(bool b){(void)b;}
void gui_synclist_layout(struct gui_synclist*l){(void)l;}
void gui_synclist_draw(struct gui_synclist*l){char buf[256];CHECK(l->callback_get_item_name);for(int i=0;i<l->nb_items;i++){
 const char*s=l->callback_get_item_name(i,l->data,buf,sizeof buf);CHECK(s);(void)strlen(s);
 if(l->callback_get_item_value){s=l->callback_get_item_value(i,l->data,buf,sizeof buf);if(s)(void)strlen(s);}
 if(l->callback_get_item_description){s=l->callback_get_item_description(i,l->data,buf,sizeof buf);if(s)(void)strlen(s);}
 if(l->callback_draw_status)l->callback_draw_status(i,l->data,&screens[0],&test_vp);
 }if(l->callback_speak_item&&l->nb_items)l->callback_speak_item(l->selected_item,l->data);}
int skinlist_redraw_status(struct screen*d,struct gui_synclist*l){(void)d;gui_synclist_draw(l);return l->nb_items;}
#ifndef FU46_REAL_ACTION
int get_action(int context,int timeout){(void)context;(void)timeout;return bridge_action(&raw_button,&held_button);}
int get_action_statuscode(int *raw){if(raw)*raw=raw_button;return (raw_button&BUTTON_REPEAT)?ACTION_REPEAT:0;}
#endif
bool list_do_action(int c,int t,struct gui_synclist*l,int*a){*a=get_action(c,t);if(*a==ACTION_STD_NEXT||*a==ACTION_STD_PREV){gui_synclist_select_item(l,l->selected_item+(*a==ACTION_STD_NEXT?1:-1));gui_synclist_draw(l);return true;}return false;}
int button_status(void){return held_button;}
bool button_get_sw_poweroff_state(void){return true;}
void button_set_sw_poweroff_state(bool b){(void)b;}
void backlight_on(void){}
bool is_backlight_on(bool b){(void)b;return true;}
void iap_agents_volume_enable(bool b){(void)b;}
void iap_agents_volume_host_keys(bool b){(void)b;}
void push_current_activity(enum current_activity a){(void)a;}
void pop_current_activity(void){}
int talk_text(const char*s,bool b){CHECK(s);(void)b;return 0;}
long default_event_handler(long a){(void)a;return 0;}
int quick_screen_current(int a,void(*p)(void)){(void)a;p();return 0;}
void usb_confirm_reopen(void){}
void splashf(int t,const char*f,...){(void)t;CHECK(f);}
unsigned sleep(unsigned t){bridge_advance(t);return 0;}
/* Keep the test driver independent of build-specific action values. */
int screen_test_action(const char *s){
#define A(x) if(!strcmp(s,#x))return x
 A(ACTION_NONE);A(ACTION_STD_MENU);A(ACTION_STD_CANCEL);A(ACTION_STD_OK);A(ACTION_STD_NEXT);A(ACTION_STD_PREV);A(ACTION_AGENTS_REMOTE_TALK);A(ACTION_AGENTS_TALK_END);
#undef A
 return -999;
}
int screen_test_button(const char*s){if(!strcmp(s,"MENU"))return BUTTON_RC_MENU|BUTTON_REL;if(!strcmp(s,"LEFT"))return BUTTON_LEFT;if(!strcmp(s,"RIGHT"))return BUTTON_RIGHT;if(!strcmp(s,"PLAY"))return BUTTON_RC_PLAY;return 0;}
int screen_test_view(void){return view;}
int screen_test_rows(void){return nrows;}
int screen_test_workspaces(void){return model.nws;}

#ifndef FU46_REAL_ACTION
void agents_shortcut_talking(bool b){(void)b;}
#endif
/* Run production Dock gesture state before attaching the actual screen. */
int screen_test_home_double(void){
#ifdef FU46_REAL_ACTION
 extern int full_action_home(void);return full_action_home();
#else
 struct agents_remote_state s={0};int fired=0;
 const int b[]={BUTTON_RC_PLAY,BUTTON_RC_PLAY|BUTTON_REL,BUTTON_RC_PLAY,BUTTON_RC_PLAY|BUTTON_REL,0};
 const int held[]={BUTTON_RC_PLAY,0,BUTTON_RC_PLAY,0,0};
 const unsigned long tick[]={100,113,128,141,162};
 for(unsigned i=0;i<5;i++)for(int j=0;j<8;j++){
  int value=agents_remote_button(&s,REMOTE_OTHER,j?0:b[i],held[i],tick[i]);
  if(value){CHECK(value==REMOTE_SWITCH_BUTTON);fired++;}
 }
 CHECK(fired==1);CHECK(!agents_remote_pending(&s));agents_nav_request(AGENTS_NAV_AGENTS,true);return fired;
#endif
}

#ifdef FU46_REAL_ACTION
#include "/home/wilf/data/PodJS/podbox.wt-disk/tools/tests/fu46_e2e/action_adapter.c"
#endif
