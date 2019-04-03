#!/usr/bin/env python3
#
# Copyright (c) 2013 Juniper Networks, Inc. All rights reserved.
#

import os
import errno
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
    output = subprocess.check_output(['unzip', '-t', zipfile], universal_newlines=True)
    lines = output.splitlines()
    for line in lines:
        print(line)
        m = re.search(r'testing:\s+([\w\-\.]+)\/', line)
        if m:
            return m.group(1)
    return None

def getFileDestination(file):
    start = file.rfind('/')
    if start < 0:
        return None
    return file[start+1:]

def ApplyPatches(pkg):
    stree_node = pkg.find('patches')
    if stree_node is None:
        return
    destination_node = pkg.find('destination')
    for patch in stree_node.getchildren():
        cmd = ['patch']
        if destination_node is not None:
            cmd.append('-d')
            cmd.append(destination_node.text)

        if patch.get('strip'):
            cmd.append('-p')
            cmd.append(patch.get('strip'))
        if ARGS['verbose']:
            print("Patching %s <%s..." % (' '.join(cmd), patch.text))
        if not ARGS['dry_run']:
            fp = open(patch.text, 'r')
            proc = subprocess.Popen(cmd, stdin = fp)
            proc.communicate()
            if not proc.returncode == 0:
                raise PatchError('Failed to apply patch %s' % patch.text)

#def VarSubst(cmdstr, filename):
#    return re.sub(r'\${filename}', filename, cmdstr)

def DownloadPackage(urls, pkg, md5):
    #Check if the package already exists
    if os.path.isfile(pkg):
        md5sum = FindMd5sum(pkg)
        if md5sum == md5:
            return
        else:
            os.remove(pkg)

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
    for distro in exclude.iterchildren('distribution'):
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

    cmd1=None

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
        if pkg.format == 'tgz':
            dest = getTarDestination(ccfile, 'z')
        elif pkg.format == 'tbz':
            dest = getTarDestination(ccfile, 'j')
        elif pkg.format == 'zip':
            dest = getZipDestination(ccfile)
        elif pkg.format == 'npm':
            dest = getTarDestination(ccfile, 'z')
        elif pkg.format == 'file':
            dest = getFileDestination(ccfile)

    rename = None
    rename_node = pkg.find('rename')
    if rename_node is not None:
        rename = rename_node.text

    if rename and os.path.isdir(rename):
        if not ARGS['dry_run']:
            # clean directory before unpacking and applying patches
            shutil.rmtree(rename)

    elif dest and os.path.isdir(dest):
        if ARGS['verbose']:
            print("Clean directory %s" % dest)
        if not ARGS['dry_run']:
            shutil.rmtree(dest)

    if unpackdir:
        try:
            os.makedirs(unpackdir)
        except OSError as exc:
            pass

    format = pkg.find('format').text
    if platform.system() == 'Windows':
        if format == 'tgz':
             ccfile1=  os.path.splitext(ccfile)[0]
             cmd = '7z x ' + ccfile + ' -o' + ARGS['cache_dir']
             cmd1 = '7z x ' + ccfile1
             if unpackdir:
                 cmd1 = cmd1 + ' -o' + unpackdir
        elif format == 'zip':
            cmd = '7z x ' + ccfile
            if unpackdir:
                 cmd = cmd + ' -o' + unpackdir
        else:
            print('Unexpected format: %s' % (pkg.format))
            return
    else:
        if format == 'tgz':
            cmd = ['tar', 'zxvf', ccfile]
        elif format == 'tbz':
            cmd = ['tar', 'jxvf', ccfile]
        elif format == 'zip':
            cmd = ['unzip', '-o', ccfile]
        elif format == 'npm':
            cmd = ['npm', 'install', ccfile, '--prefix', ARGS['cache_dir']]
        elif format == 'file':
            cmd = ['cp', '-af', ccfile, dest]
        else:
            print('Unexpected format: %s' % (format))
            return
    if not ARGS['dry_run']:
        cd = None
        if platform.system() != 'Windows':
            if unpackdir:
                cd = unpackdir
        if format == 'npm':
            try:
                os.makedirs(ARGS['node_modules_dir'])
                os.makedirs(ARGS['node_modules_tmp_dir'])
            except OSError as exc:
                if exc.errno == errno.EEXIST:
                    pass
                else:
                    print('mkdirs of ' + ARGS['node_modules_dir'] + ' ' + ARGS['node_modules_tmp_dir'] + ' failed.. Exiting..')
                    return

            npmCmd = ['cp', '-af', ARGS['node_modules_tmp_dir'] + '/' + pkg['name'],
                      ARGS['node_modules_dir']]
            if os.path.exists(ARGS['node_modules_tmp_dir'] + '/' + pkg['name']):
                cmd = npmCmd
            else:
                try:
                    p = subprocess.Popen(cmd, cwd = cd)
                    p.wait()
                    cmd = npmCmd
                except OSError:
                    print(' '.join(cmd) + ' could not be executed, bailing out!')
                    return
        p = subprocess.Popen(cmd, cwd = cd)
        p.wait()
        if cmd1: #extra stuff for windows
            p = subprocess.Popen(cmd1, cwd = cd)
            p.wait()
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
        dependencies = ['7z', 'patch']
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
