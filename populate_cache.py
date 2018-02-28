#!/usr/bin/env python
#
# This script creates a local mirror for third-party packages used
# in Contrail builds.
#
# Usage: populate_cache.py target_directory/

import logging
import argparse
import os
import hashlib
import shutil
import sys
import urllib2

from urlparse import urlparse
from lxml import objectify

LOG = logging.getLogger("populate_cache")
LOG.setLevel(logging.DEBUG)

ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)

LOG.addHandler(ch)

def parse_packages_xml(path):
    return objectify.parse(path)

def get_package_list(xml):
    root = xml.getroot()
    return root.findall("package")

def get_package_details(element):
    md5sum = element['md5']
    canonical_url = element['urls'].findall("url[@canonical='true']")
    assert len(canonical_url) == 1

    try:
        filename = element['local-filename'].text
    except AttributeError:
        parsed_url = urlparse(canonical_url[0].text)
        filename = os.path.basename(parsed_url.path)

    return (canonical_url[0].text, filename, md5sum)

def cached_file_exists(dest, filename, md5sum):
    parent_dir = os.path.join(dest, filename[0])
    local_path = os.path.join(parent_dir, filename)

    if not os.path.exists(local_path):
        LOG.info("File %s missing, will download", filename)
        return False

    with open(local_path, 'rb') as fh:
        calculated_md5 = hashlib.md5(fh.read()).hexdigest()
        if calculated_md5 != md5sum:
            LOG.warn("File %s found, but checksum mismatch "
                     "(found: '%s' expected: '%s')",
                     filename, calculated_md5, md5sum)
            return False

    LOG.info("File %s found, checksum matches", filename)
    return True

def download_package(canonical_url, dest, filename, md5sum):
    parent_dir = os.path.join(dest, filename[0])
    local_path = os.path.join(parent_dir, filename)

    if not os.path.exists(parent_dir):
        os.makedirs(parent_dir)

    chunk = 16 * 1024
    req = urllib2.urlopen(canonical_url)
    with open(local_path, 'wb') as fh:
        shutil.copyfileobj(req, fh, chunk)

    with open(local_path, 'rb') as fh:
        calculated_md5 = hashlib.md5(fh.read()).hexdigest()
        if calculated_md5 != md5sum:
            LOG.error("File %s checksum error"
                     "(found: '%s' expected: '%s')",
                     filename, calculated_md5, md5sum)
            return False
        else:
            LOG.info("File %s downloaded, checksum matches", filename)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", nargs=1)
    args = parser.parse_args(sys.argv[1:])

    dest = args.destination[0]

    xml = parse_packages_xml("packages.xml")
    packages = get_package_list(xml)
    for package in packages:
        canonical_url, filename, md5sum = get_package_details(package)
        if cached_file_exists(dest, filename, md5sum):
            continue
        download_package(canonical_url, dest, filename, md5sum)

if __name__ == "__main__":
    main()

