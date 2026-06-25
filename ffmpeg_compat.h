#ifndef FFMPEG_COMPAT_H
#define FFMPEG_COMPAT_H

#include <libswscale/swscale.h>

struct SwsContext {
    int threads;
    int flags;
};

#define sws_free_context(p) (sws_freeContext(*(p)), *(p) = NULL)

#endif
