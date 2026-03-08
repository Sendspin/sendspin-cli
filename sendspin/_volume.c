#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdint.h>
#include <string.h>

/* Fixed-point precision: 32 fractional bits */
#define FRAC_BITS 32
#define ROUND_TERM (1LL << (FRAC_BITS - 1))

static PyObject *apply_volume(PyObject *self, PyObject *args) {
    Py_buffer buf;
    int bytes_per_sample;
    unsigned long long scale;  /* fixed-point 32-bit fractional */

    if (!PyArg_ParseTuple(args, "w*iK", &buf, &bytes_per_sample, &scale))
        return NULL;

    uint8_t *data = (uint8_t *)buf.buf;
    Py_ssize_t len = buf.len;

    if (bytes_per_sample <= 0 || (len % bytes_per_sample) != 0) {
        PyBuffer_Release(&buf);
        PyErr_Format(PyExc_ValueError,
                     "buffer length (%zd) is not a multiple of bytes_per_sample (%d)",
                     len, bytes_per_sample);
        return NULL;
    }

    switch (bytes_per_sample) {
    case 1: {
        int8_t *samples = (int8_t *)data;
        for (Py_ssize_t i = 0; i < len; i++) {
            int64_t s = (int64_t)samples[i] * (int64_t)scale + ROUND_TERM;
            samples[i] = (int8_t)(s >> FRAC_BITS);
        }
        break;
    }
    case 2: {
        Py_ssize_t count = len / 2;
        for (Py_ssize_t i = 0; i < count; i++) {
            int16_t sample;
            memcpy(&sample, data + i * 2, 2);
            int64_t s = (int64_t)sample * (int64_t)scale + ROUND_TERM;
            int16_t out = (int16_t)(s >> FRAC_BITS);
            memcpy(data + i * 2, &out, 2);
        }
        break;
    }
    case 3: {
        Py_ssize_t count = len / 3;
        for (Py_ssize_t i = 0; i < count; i++) {
            uint8_t *p = data + i * 3;
            /* Unpack 24-bit LE and sign-extend to 32-bit */
            int32_t s = (int32_t)(p[0] | (p[1] << 8) | (p[2] << 16));
            if (s & 0x800000)
                s |= (int32_t)0xFF000000;
            /* Fixed-point multiply with rounding */
            int32_t out = (int32_t)(((int64_t)s * (int64_t)scale + ROUND_TERM) >> FRAC_BITS);
            /* Pack back to 24-bit LE */
            p[0] = (uint8_t)(out & 0xFF);
            p[1] = (uint8_t)((out >> 8) & 0xFF);
            p[2] = (uint8_t)((out >> 16) & 0xFF);
        }
        break;
    }
    case 4: {
        Py_ssize_t count = len / 4;
        for (Py_ssize_t i = 0; i < count; i++) {
            int32_t sample;
            memcpy(&sample, data + i * 4, 4);
            int64_t s = (int64_t)sample * (int64_t)scale + ROUND_TERM;
            int32_t out = (int32_t)(s >> FRAC_BITS);
            memcpy(data + i * 4, &out, 4);
        }
        break;
    }
    default:
        PyBuffer_Release(&buf);
        PyErr_Format(PyExc_ValueError, "unsupported bytes_per_sample: %d", bytes_per_sample);
        return NULL;
    }

    PyBuffer_Release(&buf);
    Py_RETURN_NONE;
}

static PyMethodDef volume_methods[] = {
    {"apply_volume", apply_volume, METH_VARARGS,
     "apply_volume(buf, bytes_per_sample, scale)\n\n"
     "Scale audio samples in-place using fixed-point multiplication.\n"
     "buf: writable buffer of PCM samples\n"
     "bytes_per_sample: 1 (8-bit), 2 (16-bit), 3 (24-bit), or 4 (32-bit)\n"
     "scale: fixed-point multiplier (amplitude * 2**32)"},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef volume_module = {
    PyModuleDef_HEAD_INIT,
    "_volume",
    "Fast fixed-point volume scaling for PCM audio.",
    -1,
    volume_methods
};

PyMODINIT_FUNC PyInit__volume(void) {
    return PyModule_Create(&volume_module);
}
