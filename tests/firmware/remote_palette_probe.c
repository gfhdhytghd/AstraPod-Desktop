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
#include "ui/native_theme.h"

#define HZ 100
#define HAVE_USB_PODLINK
#define COVER_HANDLE 1

void panicf(const char *fmt, ...)
{
    va_list args;
    va_start(args, fmt);
    vfprintf(stderr, fmt, args);
    va_end(args);
    abort();
}

static long current_tick;
static struct { unsigned resume_art_hash; } global_status;
static bool remote, local_available;
static struct cider_state state;
static struct cider_cache art_cache;
static unsigned int palette_reads, palette_requests;
static const char a_key[] = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
static const char b_key[] = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

struct mp3entry { char path[256]; };
static struct bitmap local_bmp, remote_bmp;
static fb_data local_px[128 * 128], remote_px[240 * 240];
static bool cover_pinned;

static int core_alloc_free_space(size_t size);
static void *core_get_data_pinned(int handle);
static void core_put_data_pinned(void *data);
struct found_palette;
static bool palette_from_cache(struct found_palette *out, unsigned *hash);
static bool cider_active(void) { return remote; }
static bool cider_palette_supported(void) { return true; }
static bool cider_available(void) { return remote; }
static unsigned cider_wps_art_generation(void) { return 0; }
static int native_wps_art_slot(void) { return 3; }
static int wps_art_handle(int slot)
{
    assert(slot == 3);
    return local_available || remote ? COVER_HANDLE : -1;
}
static bool wps_art_pin(int handle, bool pin)
{
    assert(handle == COVER_HANDLE && cover_pinned != pin);
    cover_pinned = pin;
    return true;
}
static bool wps_art_bitmap(int handle, struct bitmap **out)
{
    assert(handle == COVER_HANDLE && cover_pinned);
    *out = remote ? &remote_bmp : &local_bmp;
    return (*out)->data != NULL;
}
static int audio_status(void) { return 1; }

static bool cider_has_art(const char *key, const char *tier)
{
    return cider_cache_find(&art_cache, key, tier) >= 0;
}

static void cider_request_art(const char *key, const char *tier)
{
    assert(!strcmp(tier, "P") && !strcmp(key, state.art));
    ++palette_requests;
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

#include "extractor.inc"

static struct palette_workspace work;
static bool workspace_pinned;
static bool workspace_available = true;

static int core_alloc_free_space(size_t size)
{
    assert(size == sizeof(work));
    return workspace_available ? 1 : -1;
}

static void *core_get_data_pinned(int handle)
{
    assert(handle == 1 && !workspace_pinned);
    workspace_pinned = true;
    return &work;
}

static void core_put_data_pinned(void *data)
{
    assert(workspace_pinned && data == &work);
    workspace_pinned = false;
}

static bool palette_from_cache(struct found_palette *out, unsigned *hash)
{
    /* Emulate the on-disk reader returning the local 128px fixture. */
    if (!local_available)
        return false;
    *hash = 42;
    return compute_palette(&work, &local_bmp, out);
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
    struct ui_album_palette snapshot;
    assert(album_palette_snapshot(&snapshot));
    assert(snapshot.has_dominant && snapshot.has_accent);
    assert(snapshot.dominant == rgb888(current.dominant));
    assert(snapshot.accent == rgb888(current.accent));
    assert(snapshot.has_vivid == current.has_vivid);
    print_palette(name, &current);
    return current;
}

static bool same(struct found_palette a, struct found_palette b)
{
    return a.accent == b.accent && a.dominant == b.dominant &&
           a.has_vivid == b.has_vivid && (!a.has_vivid || a.vivid == b.vivid);
}

static void check_background(void)
{
    /* Center cropping a 4x4 cover into 4x2 must omit both outer rows. Use
     * dark text to keep the adaptive filter from altering the colors. */
    fb_data source[16] = {
        LCD_BLACK, LCD_BLACK, LCD_BLACK, LCD_BLACK,
        LCD_WHITE, LCD_WHITE, LCD_WHITE, LCD_WHITE,
        LCD_WHITE, LCD_WHITE, LCD_WHITE, LCD_WHITE,
        LCD_BLACK, LCD_BLACK, LCD_BLACK, LCD_BLACK,
    };
    struct bitmap saved = remote_bmp;
    remote_bmp.width = remote_bmp.height = 4;
    remote_bmp.data = (unsigned char *)source;
    fb_data out[8];
    for (int blur = 0; blur < 2; ++blur)
    {
        assert(album_art_render_background(out, 4, 2, COVER_HANDLE, blur, LCD_BLACK));
        for (size_t i = 0; i < ARRAYLEN(out); ++i)
            assert(out[i] == LCD_WHITE);
        assert(source[0] == LCD_BLACK && source[4] == LCD_WHITE);
    }

    /* A portrait output crops both outer columns and keeps the source stride. */
    for (size_t i = 0; i < ARRAYLEN(source); ++i)
        source[i] = (i % 4 == 0 || i % 4 == 3) ? LCD_BLACK : LCD_WHITE;
    assert(album_art_render_background(out, 2, 4, COVER_HANDLE, true, LCD_BLACK));
    for (size_t i = 0; i < ARRAYLEN(out); ++i)
        assert(out[i] == LCD_WHITE);
    assert(!cover_pinned && !workspace_pinned && !palette_lock);
    remote_bmp = saved;
}

int main(int argc, char **argv)
{
    assert(argc == 7);
    setvbuf(stdout, NULL, _IONBF, 0);
    workspace_available = false;
    album_palette_alloc();
    struct ui_album_palette empty = { .has_accent = true };
    album_palette_refresh();
    assert(!album_palette_snapshot(&empty) && !empty.has_accent);
    workspace_available = true;
    album_palette_alloc();

    read_exact(argv[1], local_px, sizeof(local_px));
    local_bmp = (struct bitmap){.width = 128, .height = 128,
                                .data = (unsigned char *)local_px};
    current_tick = 10;
    local_available = true;
    ++track_serial;
    album_palette_refresh();
    struct found_palette local = applied("local");
    assert(global_status.resume_art_hash == 42);

    FILE *medium = fopen(argv[6], "rb");
    assert(medium && fseek(medium, 12, SEEK_SET) == 0 &&
           fread(local_px, 1, sizeof(local_px), medium) == sizeof(local_px));
    fclose(medium);
    struct found_palette host_128 = {0};
    assert(compute_palette(&work, &local_bmp, &host_128));
    print_palette("B host 128", &host_128);

    remote = true;
    strlcpy(state.art, a_key, sizeof(state.art));
    publish_art(a_key, "L", argv[2]);
    publish_art(a_key, "P", argv[5]);
    album_palette_refresh();
    struct found_palette a = applied("A applied");

    strlcpy(state.art, b_key, sizeof(state.art));
    current_tick += 10;
    album_palette_refresh();
    assert(palette_requests && same(a, applied("B waiting")));
    publish_art(b_key, "L", argv[3]);
    assert(cider_copy_art(b_key, "L", remote_px, sizeof(remote_px)));
    remote_bmp = (struct bitmap){.width = 240, .height = 240,
                                 .data = (unsigned char *)remote_px};
    check_background();

    struct found_palette raw_l = {0};
    assert(compute_palette(&work, &remote_bmp, &raw_l));
    print_palette("B raw L", &raw_l);

    /* Increasing the contrast of L must never affect the P-tier palette. */
    struct img_filter invert;
    assert(img_filter_compile("invert", IMG_CLASS_LEVELS, &invert));
    img_filter_apply(remote_px, 240, 240, &invert);
    struct found_palette filtered_l = {0};
    assert(compute_palette(&work, &remote_bmp, &filtered_l));
    print_palette("B filtered L", &filtered_l);
    assert(!same(raw_l, filtered_l));

    publish_art(a_key, "P", argv[5]);
    album_palette_refresh();
    assert(same(a, applied("B waiting (late A)")));
    publish_art(b_key, "P", argv[4]);
    current_tick += 2 * HZ;
    album_palette_refresh();
    struct found_palette chosen = applied("B applied");
    assert(same(local, chosen) && "local and remote palettes diverged");
    unsigned generation = album_palette_generation();
    album_palette_refresh();
    assert(palette_reads == 2 && generation == album_palette_generation());

    state.art[0] = '\0';
    album_palette_refresh();
    current_tick += 2 * HZ;
    album_palette_refresh();
    assert(!album_palette_snapshot(&empty));
    assert(!cover_pinned && !workspace_pinned && !palette_lock);
    return 0;
}
