/* SDL-only PodLink resident peer. The Python side owns the synthetic transport. */
#include "config.h"
#include "kernel.h"
#include "podlink.h"
#include "root_menu.h"
#include "screens/playback/cider.h"
#include "screens/playback/cider_library.h"
#include <sys/socket.h>
#include <sys/un.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static const struct podlink_resident *resident;
static long stack[DEFAULT_STACK_SIZE * 6 / sizeof(long)];
static unsigned char data[PODLINK_MAX_MESSAGE];
static int link_fd = -1;

uint32_t podlink_crc32(const unsigned char *bytes, unsigned int length)
{
    uint32_t crc = ~0u;
    for (unsigned int n = 0; n < length; n++)
    {
        crc ^= bytes[n];
        for (int bit = 0; bit < 8; bit++)
            crc = (crc >> 1) ^ ((0u - (crc & 1u)) & 0xedb88320u);
    }
    return ~crc;
}

void podlink_set_resident(const struct podlink_resident *r) { resident = r; }
bool podlink_host_connected(void) { return link_fd >= 0; }
uint32_t podlink_generation(void) { return 1; }
enum podlink_connection podlink_state(void) { return PODLINK_CLOSED; }
bool podlink_open(void) { return false; }
bool podlink_open_agents(void (*cb)(const struct podlink_message *)) { (void)cb; return false; }
void podlink_close(void) {}
void podlink_shutdown(void) {}
int podlink_send(unsigned int t, const void *d, unsigned int n)
{ (void)t; (void)d; (void)n; return PODLINK_INVALID; }
int podlink_poll(struct podlink_message *m) { (void)m; return 0; }
void podlink_wait(int ticks) { sleep(ticks); }
unsigned int podlink_max_message(void) { return PODLINK_MAX_MESSAGE; }
bool podlink_take_time(char *r, unsigned long *t) { (void)r; (void)t; return false; }
bool usb_podlink_active(void) { return false; }

static void run_peer(void)
{
    unsigned int have = 0, size = 0;
    unsigned char header[3];
    while (true)
    {
        int n = recv(link_fd, size ? data + have : header + have,
                     (size ? size : sizeof header) - have, MSG_DONTWAIT);
        if (n > 0)
        {
            have += n;
            if (!size && have == sizeof header)
            {
                size = header[1] | header[2] << 8;
                have = 0;
                if (!size || size > sizeof data) abort();
            }
            else if (size && have == size)
            {
                struct podlink_message m = {.type = header[0], .length = size, .data = data};
                resident->receive(&m);
                have = size = 0;
            }
        }
        char record[512];
        unsigned int len = resident->next(record, sizeof record - 1);
        if (len)
        {
            unsigned char wire[2] = {len, len >> 8};
            if (send(link_fd, wire, sizeof wire, MSG_NOSIGNAL) != sizeof wire ||
                send(link_fd, record, len, MSG_NOSIGNAL) != (int)len)
                abort();
        }
        if (n <= 0 && !len) sleep(1);
        else yield();
    }
}

int fu79_scene(int screen)
{
    const char *path = getenv("FU79_PEER");
    const char *scene = getenv("FU79_SCENE");
    if (!path || !scene) return screen;
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    snprintf(address.sun_path, sizeof address.sun_path, "%s", path);
    link_fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (link_fd < 0 || connect(link_fd, (struct sockaddr *)&address, sizeof address))
        abort();
    if (!create_thread(run_peer, stack, sizeof stack, 0, "fu79-peer"
                       IF_PRIO(, PRIORITY_SYSTEM) IF_COP(, CPU)))
        abort();
    for (int i = 0; i < 5 * HZ && !cider_available(); i++)
        sleep(1);
    if (!cider_available()) abort();
    cider_remote_source();
    if (!strcmp(scene, "idle"))
        return screen;
    if (!strcmp(scene, "flow"))
        return GO_TO_ALBUM_COVERS;
    if (!strcmp(scene, "wps"))
        return GO_TO_WPS;
    struct cider_row row = {.title = "Fixture albums", .route = "albums"};
    if (!strcmp(scene, "tracks"))
    {
        row.kind = CIDER_ROW_ALBUM;
        snprintf(row.title, sizeof row.title, "Fixture album 00");
        snprintf(row.route, sizeof row.route, "album/fixture");
        memset(row.art, 'a', 64);
    }
    cider_browse_at(&row);
    return screen;
}

/* These physical disk-mode paths are unreachable in this fixture. */
void usb_confirm_reopen(void) {}
bool usb_disk_take_warning(void) { return false; }
