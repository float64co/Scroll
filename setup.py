import os
import shutil
from setuptools import setup, find_packages
from setuptools.command.install import install
from setuptools.command.develop import develop

_HERE        = os.path.dirname(os.path.abspath(__file__))
_SCROLL_HOME = os.path.expanduser("~/.scroll")
_SCRIPTS_DST = os.path.join(_SCROLL_HOME, "scripts")
_DOCS_DST    = os.path.join(_SCROLL_HOME, "docs")
_CONFIG_SRC  = os.path.join(_HERE, "config.hcl")
_CONFIG_DST  = os.path.join(_SCROLL_HOME, "config.hcl")
_SCRIPTS_SRC = os.path.join(_HERE, "scripts")


def _setup_scroll_home():
    for d in (_SCROLL_HOME, _SCRIPTS_DST, _DOCS_DST):
        os.makedirs(d, exist_ok=True)

    if not os.path.exists(_CONFIG_DST) and os.path.exists(_CONFIG_SRC):
        shutil.copy(_CONFIG_SRC, _CONFIG_DST)
        print("scroll: installed default config to %s" % _CONFIG_DST)

    if os.path.isdir(_SCRIPTS_SRC):
        for fname in sorted(os.listdir(_SCRIPTS_SRC)):
            if fname.endswith(".py"):
                dst = os.path.join(_SCRIPTS_DST, fname)
                if not os.path.exists(dst):
                    shutil.copy(os.path.join(_SCRIPTS_SRC, fname), dst)
                    print("scroll: installed script %s" % fname)


class _PostInstall(install):
    def run(self):
        super().run()
        _setup_scroll_home()


class _PostDevelop(develop):
    def run(self):
        super().run()
        _setup_scroll_home()


setup(
    name="scroll",
    version="0.0.3",
    packages=find_packages(),
    package_data={"scroll": ["docs/*.txt"]},
    entry_points={
        "console_scripts": [
            "scroll=scroll.__main__:main",
        ],
    },
    python_requires=">=3.7",
    cmdclass={
        "install": _PostInstall,
        "develop": _PostDevelop,
    },
)
