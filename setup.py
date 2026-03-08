"""Build C extensions for sendspin."""

from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext


class OptionalBuildExt(build_ext):
    """A build_ext that treats C extensions as optional.

    If compilation fails (e.g. missing compiler or headers), the error is
    reported but does not abort the build, allowing the numpy fallback to be
    used at runtime.
    """

    def run(self) -> None:
        try:
            super().run()
        except Exception as exc:
            print(
                "WARNING: Building C extensions for sendspin failed; "
                "falling back to numpy implementation. "
                f"Error: {exc}",
            )

    def build_extension(self, ext: Extension) -> None:
        try:
            super().build_extension(ext)
        except Exception as exc:
            print(
                f"WARNING: Failed to build extension {ext.name!r}; "
                "continuing without it. "
                f"Error: {exc}",
            )


setup(
    ext_modules=[
        Extension("sendspin._volume", sources=["sendspin/_volume.c"]),
    ],
    cmdclass={"build_ext": OptionalBuildExt},
)
