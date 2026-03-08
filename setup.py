"""Build C extensions for sendspin."""

from setuptools import Extension, setup

setup(
    ext_modules=[
        Extension("sendspin._volume", sources=["sendspin/_volume.c"]),
    ],
)
