/* SPDX-License-Identifier: GPL-2.0-or-later */
#include <assert.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "system.h"
#include "draw/bmp.h"
#include "draw/color.h"
#include "draw/img_filter.h"
#include "screens/playback/cider_model.h"

#define HZ 100
#define DYNAMIC_BG_LIGHT 1
#define DYNAMIC_BG_DARK 2
#define COLOR_WORDS_MAX 64
#define CIDER_ART_HANDLE 0x7ffffffe
#define HAVE_USB_PODLINK
#define WPS 1
#define ACTIVITY_WPS 1
#define ACTIVITY_LYRICS 2
#define AUDIO_STATUS_PAUSE 2

void panicf(const char *fmt, ...)
{
    va_list args;
    va_start(args, fmt);
    vfprintf(stderr, fmt, args);
    va_end(args);
    abort();
}

static long current_tick;
static struct { unsigned resume_art_hash; long last_volume_change; } global_status;
static struct {
    bool dynamic_colors;
    int dynamic_colors_background;
    unsigned fg_color;
} global_settings = {true, 0, 0};

static bool remote;
static struct cider_state incoming;
static struct cider_cache art_cache;
static int palette_requests;
static unsigned int palette_reads;
static const char a_key[] = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
static const char b_key[] = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
static bool cider_active(void) { return remote; }
static bool cider_palette_supported(void) { return true; }
static bool wps_art_pin(int handle, bool pin);
static bool wps_art_bitmap(int handle, struct bitmap **out);
static int wps_art_handle(int slot);
static bool palette_from_cache(void);

static bool cider_has_art(const char *key, const char *tier)
{
    return cider_cache_find(&art_cache, key, tier) >= 0;
}

static void cider_request_art(const char *key, const char *tier)
{
    if (!strcmp(tier, "P"))
    {
        assert(!strcmp(key, incoming.art));
        palette_requests++;
    }
}

static int palette_lock;
static void mutex_lock(int *m) { assert(!*m); ++*m; }
static void mutex_unlock(int *m) { assert(*m == 1); --*m; }
#define cider_mutex palette_lock_for_copy
#define cache art_cache
static int palette_lock_for_copy;
static void *copy_art_bytes(void *dst, const void *src, size_t bytes)
{
    if (bytes == 128 * 128 * 2)
        ++palette_reads;
    return memcpy(dst, src, bytes);
}

#define memcpy copy_art_bytes
#include "copy_art.inc"

#undef memcpy
#undef cache
#undef cider_mutex

static struct mp3entry {
    char *title, *artist, *albumartist, *album, *composer;
    char path[256];
    unsigned long length, elapsed;
} local_id3;
struct wps_state { struct mp3entry *id3, *nid3; bool paused; };
static struct mp3entry *audio_current_track(void) { return &local_id3; }
static struct mp3entry *audio_next_track(void) { return NULL; }
static int audio_status(void) { return 0; }
static int playback_current_aa_hid(int slot) { (void)slot; return -1; }
static bool buf_pin_handle(int h, bool pin) { (void)h; (void)pin; return false; }
static int bufgetdata(int h, int off, void **p) { (void)h; (void)off; (void)p; return -1; }
static int get_current_activity(void) { return ACTIVITY_WPS; }
static void cider_snapshot(struct cider_state *state) { *state = incoming; }
static void skin_request_full_update(int screen) { (void)screen; }
static void lyrics_remote_cancel(void) {}
static void lyrics_remote_track(const char *id, uint32_t generation, uint32_t library,
                                unsigned long duration)
{
    (void)id; (void)generation; (void)library; (void)duration;
}
static void cider_warm_art(const char *const *keys, unsigned int count, const char *tier)
{
    (void)keys; (void)count; (void)tier;
}
#define cider_retain_art cider_warm_art
#define cider_prefetch_art cider_request_art
void skin_albumart_art_opened(int handle);
struct skin_albumart {
    struct img_filter filter;
    int filtered_art, filter_avoid;
    unsigned filtered_gen;
};
static unsigned dynamic_colors_resolve(unsigned colour) { return colour; }
static void render_filtered(struct skin_albumart *aa, int handle, struct bitmap *bmp)
{
    (void)aa; (void)handle; (void)bmp;
    abort(); /* This probe uses the real in-place filter path. */
}

#include "wps.inc"
#include "extractor.inc"

static struct bitmap local_bmp;
static fb_data local_px[128 * 128];

static bool palette_from_cache(void)
{
    struct found_palette out = {0};
    if (!compute_palette(&local_bmp, &out))
        return false;
    apply_found(&out);
    return true;
}

static void read_exact(const char *path, void *out, size_t size)
{
    FILE *in = fopen(path, "rb");
    assert(in && fread(out, 1, size, in) == size && fgetc(in) == EOF);
    fclose(in);
}

static void publish_art(const char *key, const char *tier, const char *path)
{
    size_t size = 12 + (!strcmp(tier, "L") ? 240 * 240 : 128 * 128) * 2;
    unsigned char *data = malloc(size);
    assert(data);
    read_exact(path, data, size);
    assert(cider_cache_put(&art_cache, key, tier, data, size, false));
    free(data);
}

static void print_palette(const char *name, const struct found_palette *out)
{
    printf("%s %04x %04x ", name, out->dominant, out->accent);
    if (out->has_vivid)
        printf("%04x\n", out->vivid);
    else
        puts("-");
}

static struct found_palette applied(const char *name)
{
    struct found_palette out = {cache.found_accent, cache.found_dominant,
                                cache.vivid, cache.has_vivid};
    print_palette(name, &out);
    return out;
}

static bool same(struct found_palette a, struct found_palette b)
{
    return a.accent == b.accent && a.dominant == b.dominant &&
           a.has_vivid == b.has_vivid && (!a.has_vivid || a.vivid == b.vivid);
}

static void filter_l(struct skin_albumart *aa)
{
    skin_albumart_filter(0, aa);
    assert(art_filtered(CIDER_ART_HANDLE));
}

int main(int argc, char **argv)
{
    assert(argc == 7);
    setvbuf(stdout, NULL, _IONBF, 0);
    read_exact(argv[1], local_px, sizeof local_px);
    local_bmp = (struct bitmap){.width = 128, .height = 128,
                              .data = (unsigned char *)local_px};
    current_tick = 10;
    needs_extraction = true;
    dynamic_colors_check_extraction(-1);
    struct found_palette local = applied("local");
    /* M uses the same host scaler and dither as L at the local sample size. */
    FILE *medium = fopen(argv[6], "rb");
    assert(medium && fseek(medium, 12, SEEK_SET) == 0 &&
           fread(local_px, 1, sizeof local_px, medium) == sizeof local_px);
    fclose(medium);
    struct found_palette host_128 = {0};
    assert(compute_palette(&local_bmp, &host_128));
    print_palette("B host 128", &host_128);


    struct skin_albumart aa = {0};
    assert(img_filter_compile("invert", IMG_CLASS_LEVELS, &aa.filter));
    struct wps_state wps = {0};
    remote = true;
    incoming.available = true;
    strcpy(incoming.art, a_key);
    publish_art(a_key, "L", argv[2]);
#ifdef TEST_PALETTE_TIER
    publish_art(a_key, "P", argv[5]);
#endif
    cider_wps_update(&wps);
    dynamic_colors_check_extraction(0);
    struct found_palette a = applied("A applied");
    filter_l(&aa);

    strcpy(incoming.art, b_key);
    current_tick += 10;
    cider_wps_update(&wps);
    dynamic_colors_check_extraction(0);
    publish_art(b_key, "L", argv[3]);
    cider_wps_update(&wps);
    assert(wps_art_handle(0) == CIDER_ART_HANDLE);
    assert(!art_filtered(CIDER_ART_HANDLE));
    struct bitmap *surface;
    assert(wps_art_bitmap(CIDER_ART_HANDLE, &surface));
    struct found_palette raw_l = {0};
    assert(compute_palette(surface, &raw_l));
    print_palette("B raw L", &raw_l);
    dynamic_colors_check_extraction(0);
#ifdef TEST_PALETTE_TIER
    assert(palette_requests && same(a, applied("B waiting")));
#endif

    filter_l(&aa);
    struct found_palette filtered_l = {0};
    assert(compute_palette(surface, &filtered_l));
    print_palette("B filtered L", &filtered_l);
    assert(!same(raw_l, filtered_l));

#ifdef TEST_PALETTE_TIER
    /* Re-send A while B's original thumbnail is still pending. */
    publish_art(a_key, "P", argv[5]);
    dynamic_colors_check_extraction(0);
    assert(same(a, applied("B waiting (late A)")));
    publish_art(b_key, "P", argv[4]);
    current_tick += 2 * HZ;
#else
    (void)a;
    ++current_tick;
#endif
    dynamic_colors_check_extraction(0);
    struct found_palette chosen = applied("B applied");
    assert(same(local, chosen) && "local and remote palettes diverged");
#ifdef TEST_PALETTE_TIER
    unsigned int reads = palette_reads;
    strcpy(incoming.title, "Another track sharing B artwork");
    cider_wps_update(&wps);
    dynamic_colors_check_extraction(0);
    assert(reads == 2 && palette_reads == reads);
#endif
    return 0;
}
