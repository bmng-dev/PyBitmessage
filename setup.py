#!/usr/bin/env python2.7

from setuptools import setup, Extension

from src.version import softwareVersion

if __name__ == "__main__":
    with open('README.md') as f:
        README = f.read()

    dist = setup(
        name='pybitmessage',
        version=softwareVersion,
        description="Reference client for Bitmessage: "
        "a P2P communications protocol",
        long_description=README,
        license='MIT',
        url='https://bitmessage.org',
        install_requires=['msgpack-python'],
        extras_require={
            'qrcode': ['qrcode'],
            'pyopencl': ['pyopencl']
        },
        classifiers=[
            "License :: OSI Approved :: MIT License",
            "Operating System :: OS Independent",
            "Programming Language :: Python :: 2.7 :: Only",
            "Topic :: Internet",
            "Topic :: Security :: Cryptography",
            "Topic :: Software Development :: Libraries :: Python Modules",
        ],
        package_dir={'pybitmessage': 'src'},
        packages=[
            'pybitmessage',
            'pybitmessage.bitmessageqt',
            'pybitmessage.bitmessagecurses',
            'pybitmessage.messagetypes',
            'pybitmessage.network',
            'pybitmessage.pyelliptic',
            'pybitmessage.socks',
            'pybitmessage.plugins'
        ],
        package_data={
            'pybitmessage': ['bitmsghash/*.cl', 'sslkeys/*.pem'],
            'pybitmessage.bitmessageqt': ['*.ui', '../translations/*.qm'],
        },
        ext_modules=[
            Extension(
                name='pybitmessage.bitmsghash.bitmsghash',
                sources=['src/bitmsghash/bitmsghash.cpp'],
                libraries=['crypto']
            )
        ],
        zip_safe=False,
        entry_points={
            'gui.menu': [
                'popMenuYourIdentities.qrcode = '
                'pybitmessage.plugins.qrcodeui [qrcode]'
            ],
            #'console_scripts': ['pybitmessage = pybitmessage.bitmessagemain:main'],
        },
        scripts=['src/pybitmessage']
    )
