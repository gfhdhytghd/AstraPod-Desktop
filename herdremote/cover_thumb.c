/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Read a seekable artwork stream and emit a native 128px RGB565 thumbnail. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

/* The simulator decoder calls these ports with a single inherited file. */
int sim_open(const char *path, int flags, ...)
{
    (void)path;
    (void)flags;
    return -1;
}

int sim_close(int fd) { return close(fd); }
ssize_t sim_read(int fd, void *buf, size_t n) { return read(fd, buf, n); }
off_t sim_lseek(int fd, off_t off, int whence) { return lseek(fd, off, whence); }
off_t sim_ffilesize(int fd)
{
    struct stat st;
    return fstat(fd, &st) ? -1 : st.st_size;
}

void yield(void) {}
void debugf(const char *fmt, ...) { (void)fmt; }
void panicf(const char *fmt, ...) { (void)fmt; abort(); }

#include "system.h"
#include "draw/image_load.h"
#include "metadata/art_sizes.h"

#define COVER_DIM 128
#define COVER_BUFFER (1024 * 1024)

int main(void)
{
    const struct image_src src = {.fd = STDIN_FILENO, .len = -1};
    struct bitmap bmp = {.format = FORMAT_NATIVE};
    bmp.data = malloc(COVER_BUFFER);
    if (!bmp.data)
        return 1;

    const int fmt = FORMAT_NATIVE | FORMAT_RESIZE | FORMAT_DITHER;
    int rc = image_load(&src, &bmp, COVER_BUFFER,
                        FORMAT_NATIVE | FORMAT_RETURN_SIZE, NULL);
    int w, h;
    if (rc <= 0 || bmp.width > 4096 || bmp.height > 4096 ||
        !aa_cover_dim(bmp.width, bmp.height, COVER_DIM, &w, &h))
        return 1;

    bmp.width = w;
    bmp.height = h;
    rc = image_load(&src, &bmp, COVER_BUFFER, fmt, NULL);
    if (rc <= 0 || bmp.width != w || bmp.height != h)
        return 1;

    const fb_data *pixels = (const fb_data *)bmp.data;
    for (int y = 0; y < COVER_DIM; y++)
        for (int x = 0; x < COVER_DIM; x++)
        {
            unsigned p = pixels[(y + (h - COVER_DIM) / 2) * w +
                                x + (w - COVER_DIM) / 2];
            putchar(p & 255);
            putchar(p >> 8);
        }
    free(bmp.data);
    return fflush(stdout) == EOF;
}
