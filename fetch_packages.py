#!/usr/bin/python
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

_RETRIES = 5
_OPT_VERBOSE = None
_OPT_DRY_RUN = None
_PACKAGE_CACHE='/tmp/cache/' + os.environ['USER'] + '/third_party'
_NODE_MODULES='./node_modules'
_TMP_NODE_MODULES=_PACKAGE_CACHE + '/' + _NODE_MODULES

from lxml import objectify

def getFilename(pkg, url):
    element = pkg.find("local-filename")
    if element:
        return str(element)

    (path, filename) = url.rsplit('/', 1)
    m = re.match(r'\w+\?\w+=(.*)', filename)
    if m:
        filename = m.group(1)
    return filename

def getTarDestination(tgzfile, compress_flag):
    cmd = subprocess.Popen(['tar', compress_flag + 'tf', tgzfile],
                           stdout=subprocess.PIPE)
    (output, _) = cmd.communicate()
    (first, _) = output.split('\n', 1)
    fields = first.split()
    return fields[0]

def getZipDestination(tgzfile):
    cmd = subprocess.Popen(['unzip', '-t', tgzfile],
                           stdout=subprocess.PIPE)
    (output, _) = cmd.communicate()
    lines = output.split('\n')
    for line in lines:
        print line
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
    stree = pkg.find('patches')
    if stree is None:
        return
    for patch in stree.getchildren():
        cmd = ['patch']
        if patch.get('strip'):
            cmd.append('-p')
            cmd.append(patch.get('strip'))
        if _OPT_VERBOSE:
            print "Patching %s <%s..." % (' '.join(cmd), str(patch))
        if not _OPT_DRY_RUN:
            fp = open(str(patch), 'r')
            proc = subprocess.Popen(cmd, stdin = fp)
            proc.communicate()

#def VarSubst(cmdstr, filename):
#    return re.sub(r'\${filename}', filename, cmdstr)

def DownloadPackage(url, pkg, md5):
    #Check if the package already exists
    if os.path.isfile(pkg):
        md5sum = FindMd5sum(pkg)
        if md5sum == md5:
            return
        else:
            os.remove(pkg)

    retry_count = 0
    while True:
	subprocess.call(['wget', '--no-check-certificate', '-O', pkg, url, '--timeout=10'])
        md5sum = FindMd5sum(pkg)
        if _OPT_VERBOSE:
            print "Calculated md5sum: %s" % md5sum
            print "Expected md5sum: %s" % md5
        if md5sum == md5:
            return
        elif retry_count <= _RETRIES:
            os.remove(pkg)
            retry_count += 1
            sleep(1)
            continue
        else:
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
    return VersionMatch(str(system[1]), str(spec[1]))
    
def PlatformRequires(pkg):
    platform = pkg.find('platform')
    if platform is None:
        return True

    info = PlatformInfo()

    exclude = platform.find('exclude')
    for distro in exclude.iterchildren('distribution'):
        name = distro.find('name')
        version = distro.find('version')
        if PlatformMatch(info, (name, version)):
            return False

    return True

def ProcessPackage(pkg):
    if not PlatformRequires(pkg):
        return

    print "Processing %s ..." % (pkg['name'])
    url = str(pkg['url'])
    filename = getFilename(pkg, url)
    ccfile = _PACKAGE_CACHE + '/' + filename
    DownloadPackage(url, ccfile, pkg.md5)

    #
    # Determine the name of the directory created by the package.
    # unpack-directory means that we 'cd' to the given directory before
    # unpacking.
    #
    dest = None
    unpackdir = pkg.find('unpack-directory')
    if unpackdir:
        dest = str(unpackdir)
    else:
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

    #
    # clean directory before unpacking and applying patches
    #
    rename = pkg.find('rename')
    if rename and os.path.isdir(str(rename)):
        if not _OPT_DRY_RUN:
            shutil.rmtree(str(rename))

    elif dest and os.path.isdir(dest):
        if _OPT_VERBOSE:
            print "Clean directory %s" % dest
        if not _OPT_DRY_RUN:
            shutil.rmtree(dest)

    if unpackdir:
        try:
            os.makedirs(str(unpackdir))
        except OSError as exc:
            pass
        

    if pkg.format == 'tgz':
        cmd = ['tar', 'zxvf', ccfile]
    elif pkg.format == 'tbz':
        cmd = ['tar', 'jxvf', ccfile]
    elif pkg.format == 'zip':
        cmd = ['unzip', '-o', ccfile]
    elif pkg.format == 'npm':
        cmd = ['npm', 'install', ccfile, '--prefix', _PACKAGE_CACHE]
    elif pkg.format == 'file':
        cmd = ['cp', '-af', ccfile, dest]
    else:
        print 'Unexpected format: %s' % (pkg.format)
        return

    if not _OPT_DRY_RUN:
        cd = None
        if unpackdir:
            cd = str(unpackdir)
        if pkg.format == 'npm':
            try:
                os.makedirs(_NODE_MODULES)
                os.makedirs(_TMP_NODE_MODULES)
            except OSError as exc:
                if exc.errno == errno.EEXIST:
                    pass
                else:
                    print 'mkdirs of ' + _NODE_MODULES + ' ' + _TMP_NODE_MODULES + ' failed.. Exiting..'
                    return

            npmCmd = ['cp', '-af', _TMP_NODE_MODULES + '/' + pkg['name'],
                      './node_modules/']
            if os.path.exists(_TMP_NODE_MODULES + '/' + pkg['name']):
                cmd = npmCmd
            else:
		try:
                   p = subprocess.Popen(cmd, cwd = cd)
                   p.wait()
                   cmd = npmCmd
		except OSError:
		   print ' '.join(cmd) + ' could not be executed, bailing out!'
		   return
        p = subprocess.Popen(cmd, cwd = cd)
        p.wait()

    if rename and dest:
        os.rename(dest, str(rename))
        dest = str(rename)

    ApplyPatches(pkg)

    autoreconf = pkg.find('autoreconf')
    if autoreconf and str(autoreconf).lower() == 'true':
        ReconfigurePackageSources(dest)

def FindMd5sum(anyfile):
    # MD5 command is different on FreeBSD systems
    if sys.platform.startswith('freebsd'):
        cmd = ['md5']
        cmd.append('-q')
    else:
        cmd = ['md5sum']
    cmd.append(anyfile)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stdin=subprocess.PIPE)
    stdout, stderr = proc.communicate()
    md5sum = stdout.split()[0]
    return md5sum

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", dest="filename",default="packages.xml",
                      help="read data from FILENAME")
    parser.add_argument("--dir", dest="dirname",
                      help="dir ")
    return parser.parse_args()

def main(filename):
    tree = objectify.parse(filename)
    root = tree.getroot()

    for object in root.iterchildren():
        if object.tag == 'package':
            ProcessPackage(object)

if __name__ == '__main__':
    dependencies = [
        'autoconf',
        'automake',
        'bzip2',
        'libtool',
        'patch',
        'unzip',
        'wget',
    ]
    for exc in dependencies:
        if not find_executable(exc):
            print 'Please install %s' % exc
            sys.exit(1)

    args = parse_args()

    os.chdir(os.path.dirname(os.path.realpath(__file__)))
    try:
        os.makedirs(_PACKAGE_CACHE)
    except OSError:
        pass

    main(args.filename)
