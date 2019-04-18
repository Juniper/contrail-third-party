#!/usr/bin/env python3
#
# Copyright (c) 2013 Juniper Networks, Inc. All rights reserved.
#

import os
import platform
import re
import shutil
import subprocess
import sys
from time import sleep
from distutils.spawn import find_executable
import argparse
import tempfile
import hashlib
import urllib.request
import xml.etree.ElementTree

# arguments (given by command line or defaults)
ARGS = dict()
if platform.system() == 'Windows':
    ARGS['filename'] = 'windows_packages.xml'
else:
    ARGS['filename'] = 'packages.xml'

ARGS['cache_dir'] = tempfile.mkdtemp()
ARGS['node_modules_dir'] = 'node_modules'
ARGS['node_modules_tmp_dir'] = ARGS['cache_dir'] + '/' + ARGS['node_modules_dir']
ARGS['verbose'] = False
ARGS['dry_run'] = False

_RETRIES = 5


class PatchError(Exception):
    pass


def getFilename(pkg, url):
    element_node = pkg.find("local-filename")
    if element_node is not None:
        return element_node.text

    (path, filename) = url.rsplit('/', 1)
    m = re.match(r'\w+\?\w+=(.*)', filename)
    if m:
        filename = m.group(1)
    return filename

def getTarDestination(tgzfile, compress_flag):
    output = subprocess.check_output(['tar', compress_flag + 'tf', tgzfile])
    first = output.splitlines()[0]
    fields = first.split()
    return fields[0]

def getZipDestination(zipfile):
    unzip_cmd = ['unzip', '-t', zipfile]
    output = subprocess.check_output(unzip_cmd, universal_newlines=True)

    lines = output.splitlines()
    for line in lines:
        print(line)
        m = re.search(r'testing:\s+([\w\-\.]+)\/', line)
        if m:
            return m.group(1)
    return None

def ApplyPatches(pkg):
    stree_node = pkg.find('patches')
    if stree_node is None:
        return
    destination_node = pkg.find('destination')
    for patch in stree_node.getchildren():
        cmd = ['patch', '-i', patch.text]

        if platform.system() == 'Windows':
            # ignore different line endings
            cmd.append('--binary')

        if destination_node is not None:
            cmd.append('-d')
            cmd.append(destination_node.text)

        if patch.get('strip'):
            cmd.append('-p')
            cmd.append(patch.get('strip'))

        if ARGS['verbose']:
            print('Patching ' + ' '.join(cmd))

        if not ARGS['dry_run']:
            proc = subprocess.run(cmd)
            if proc.returncode != 0:
                raise PatchError('Failed to apply patch %s' % patch.text)

def DownloadPackage(urls, pkg, md5):
    retry_count = 0
    while retry_count <= _RETRIES:
        for url in urls:
            # poor man's templating
            url = url.text
            if "{{ site_mirror }}" in url:
                if not ARGS['site_mirror']:
                    continue
                url = url.replace("{{ site_mirror }}", ARGS['site_mirror'])

            try:
                urllib.request.urlretrieve(url, pkg)
            except:
                print("Url did not work: " + url)
                continue

            md5sum = FindMd5sum(pkg)
            if ARGS['verbose']:
                print("Calculated md5sum: %s" % md5sum)
                print("Expected md5sum: %s" % md5)
            if md5sum == md5:
                return
            os.remove(pkg)
        retry_count += 1
        # back-off retry timer - worst case scenario we wait for 150 seconds
        sleep(10 * retry_count)

    # We couldn't download the package, return the last md5sum
    raise RuntimeError("MD5sum %s, expected(%s) dosen't match for the "
                       "downloaded package %s" % (md5sum, md5, pkg))


def ReconfigurePackageSources(path):
    """Run autoreconf tool from GNU Autotools suite.

    Some packages' Makefile.am files are patched after being dowloaded (like
    thirf). The configure script has to be regenerated in this case. Since
    there might be differences in version of aclocal, autoconf and automake
    tools used while preparing the package's sources and those present on the
    installation host, autoreconf should be run on the pathed sources before
    running configure && make && make install commands.
    """
    proc = subprocess.Popen(['autoreconf', '--force', '--install'],
                            cwd=path)
    ret = proc.wait()
    if ret is not 0:
        sys.exit('Terminating: autoreconf returned with error code: %d', ret)

def PlatformInfo():
    (distname, version, _) = platform.dist()
    return (distname.lower(), version)

def VersionMatch(v_sys, v_spec):
    from distutils.version import LooseVersion
    """
    Returns True if the system version matches the specified version.
       version_spec := -version | version | version+
       version := [0-9]+(\.[0-9]+)*
    """
    if v_spec.find('+') >= 0:
        return LooseVersion(v_sys) >= LooseVersion(v_spec[:-1])
    elif v_spec.find('-') >= 0:
        return LooseVersion(v_sys) <= LooseVersion(v_spec[1:])
    else:
        return LooseVersion(v_sys) == LooseVersion(v_spec)

def PlatformMatch(system, spec):
    if system[0] != spec[0]:
        return False
    return VersionMatch(system[1], spec[1])
    
def PlatformRequires(pkg):
    platform = pkg.find('platform')
    if platform is None:
        return True

    info = PlatformInfo()

    exclude = platform.find('exclude')
    for distro in exclude.findall('distribution'):
        name = distro.find('name').text
        version = distro.find('version').text
        if PlatformMatch(info, (name, version)):
            return False

    return True

def ProcessPackage(pkg):
    if not PlatformRequires(pkg):
        return

    print("Processing %s ..." % (pkg.find('name').text))
    urls = list(pkg.find('urls'))
    filename = getFilename(pkg, urls[0].text)
    ccfile = ARGS['cache_dir'] + '/' + filename
    DownloadPackage(urls, ccfile, pkg.find('md5').text)

    pkg_format = None
    pkg_format_node = pkg.find('format')
    if pkg_format_node is not None:
        pkg_format = pkg_format_node.text

    #
    # Determine the name of the directory created by the package.
    # unpack-directory means that we 'cd' to the given directory before
    # unpacking.
    #
    dest = None
    unpackdir = None
    unpackdir_node = pkg.find('unpack-directory')
    destination_node = pkg.find('destination')
    if unpackdir_node is not None:
        unpackdir = unpackdir_node.text
        dest = unpackdir
    elif destination_node is not None:
        dest = destination_node.text
    elif platform.system() != 'Windows':
        if pkg_format == 'tgz':
            dest = getTarDestination(ccfile, 'z')
        elif pkg_format == 'tbz':
            dest = getTarDestination(ccfile, 'j')
        elif pkg_format == 'zip':
            dest = getZipDestination(ccfile)

    rename = None
    rename_node = pkg.find('rename')
    if rename_node is not None:
        rename = rename_node.text

    shutil_format = None
    if pkg_format == 'tgz':
        shutil_format = 'gztar'
    elif pkg_format == 'tbz':
        shutil_format = 'bztar'
    elif pkg_format == 'zip':
        shutil_format = 'zip'

    if not ARGS['dry_run']:
        if rename and os.path.isdir(rename):
            # clean directory before unpacking and applying patches
            shutil.rmtree(rename)
        elif dest and os.path.isdir(dest):
            if ARGS['verbose']:
                print("Clean directory %s" % dest)
            shutil.rmtree(dest)

        shutil.unpack_archive(ccfile, unpackdir, shutil_format)

    if rename and dest:
        os.rename(dest, rename)
        dest = rename

    ApplyPatches(pkg)

    autoreconf = pkg.find('autoreconf')
    if autoreconf is not None and autoreconf.text.lower() == 'true':
        ReconfigurePackageSources(dest)

def FindMd5sum(anyfile):
    hash_md5 = hashlib.md5()
    with open(anyfile, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()


def parse_args():
    global ARGS
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", dest="filename",default=ARGS['filename'],
                      help="read data from FILENAME")
    parser.add_argument("--cache-dir", default=ARGS['cache_dir'])
    parser.add_argument("--node-module-dir", default=ARGS['node_modules_dir'])
    parser.add_argument("--node-module-tmp-dir", default=ARGS['node_modules_tmp_dir'])
    parser.add_argument("--verbose", default=ARGS['verbose'], action='store_true')
    parser.add_argument("--dry-run", default=ARGS['dry_run'], action='store_true')
    parser.add_argument("--site-mirror", dest="site_mirror", required=False, default=None)
    ARGS = vars(parser.parse_args())


def main():
    tree = xml.etree.ElementTree.parse(ARGS['filename'])
    root = tree.getroot()

    for object in root:
        if object.tag == 'package':
            ProcessPackage(object)

if __name__ == '__main__':
    parse_args()
    if platform.system() == 'Windows':
        dependencies = ['patch']
    else:
        dependencies = [
            'autoconf',
            'automake',
            'bzip2',
            'libtool',
            'patch',
            'unzip',
        ]

    for exc in dependencies:
        if not find_executable(exc):
            print('Please install %s' % exc)
            sys.exit(1)

    os.chdir(os.path.dirname(os.path.realpath(__file__)))
    try:
        os.makedirs(ARGS['cache_dir'])
    except OSError:
        pass

    main()
