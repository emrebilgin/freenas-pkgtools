from __future__ import print_function
import hashlib
import logging
import os
import re
import sys
import tempfile
import time
import socket
import ssl
import six

import six.moves.configparser as configparser

from http.client import REQUESTED_RANGE_NOT_SATISFIABLE as HTTP_RANGE
from http.client import NOT_FOUND as HTTP_NOT_FOUND

from . import (
    Avatar, UPDATE_SERVER, MASTER_UPDATE_SERVER, Exceptions,
    Installer, Train, Package, Manifest, DEFAULT_CA_FILE
)

from stat import (
    S_ISDIR, S_ISCHR, S_ISBLK, S_ISREG, S_ISFIFO, S_ISLNK, S_ISSOCK,
    S_IMODE
)

VERIFY_SKIP_PATHS = [
    '/var/',
    '/etc',
    '/dev',
    '/conf/base/etc/master.passwd',
    '/compat/linux/proc',
    '/boot/zfs/zpool.cache',
    '/usr/local/share/smartmontools/drivedb.h',
    '/boot/device.hints',
    '/usr/local/lib/perl5/5.16/man/whatis',
    '/usr/share/man'
]
CONFIG_DEFAULT = "Defaults"
CONFIG_SEARCH = "Search"
CONFIG_SERVER = "update_server"

UPDATE_SERVER_NAME_KEY = "name"
UPDATE_SERVER_MASTER_KEY = "master"
UPDATE_SERVER_URL_KEY = "url"
UPDATE_SERVER_SIGNED_KEY = "signing"

TRAIN_DESC_KEY = "Descripton"
TRAIN_SEQ_KEY = "Sequence"
TRAIN_CHECKED_KEY = "LastChecked"

log = logging.getLogger('freenasOS.Configuration')

# List of trains
TRAIN_FILE = "trains.txt"

def CheckFreeSpace(path=None, pool=None, required=0):
    """
    Check for enough free space on the path/pool.
    If pool is given, or path is on a zfs pool, we'll
    also check to see if it'll fit.  ("fit" is a bit
    complicated there -- we want to ensure we don't go
    over a maximum percentage, unless the amount of data
    required is less than a reasonable multiple of the free
    space.)
    
    Returns True if it's okay, False otherwise.
    """
    import libzfs
    from bsd import statfs
    # Don't let it get above 90% used for zfs
    zfs_max_pct = 90
    # Unless the free space is at least 4x the required size
    zfs_multiple = 4

    log.debug("CheckFreeSpace(path={}, pool={}, required={})".format(path, pool, required))
    
    if path == pool and path is None:
        raise ValueError("One of path or pool must be set")
    if path is not None and pool is not None:
        raise ValueError("Only one of path or pool may be set")

    # First we check the path, if given
    if path:
        mntpoint = statfs(path)
        required_blocks = int(required / mntpoint.blocksize)
        if required_blocks > mntpoint.free_blocks:
            log.debug("Required blocks ({}) > free blocks ({})".format(required_blocks, mntpoint.free_blocks))
            return False
        if mntpoint.fstype == "zfs" and not pool:
            pool = mntpoint.source.split("/")[0]
            
    if pool:
        with libzfs.ZFS() as zfs:
            p = zfs.get(pool)
            pool_size = p.properties["size"].parsed
            pool_used = p.properties["allocated"].parsed
            pool_free = p.properties["free"].parsed
        pool_max = int(pool_size * (zfs_max_pct / 100.0))
        if (pool_used + required) >= pool_max:
            if pool_free > (zfs_multiple * required):
                return True
            log.debug("pool_used ({}) + required ({}) > pool_max ({})".format(pool_used, required, pool_max))
            return False
    return True

def ChecksumFile(fobj):
    # Produce a SHA256 checksum of a file.
    # Read it in chunk
    def readchunk():
        chunksize = 1024 * 1024
        return fobj.read(chunksize)
    hash = hashlib.sha256()
    fobj.seek(0)
    for piece in iter(readchunk, b''):
        hash.update(piece)
    fobj.seek(0)
    return hash.hexdigest()


def TryOpenFile(path):
    try:
        f = open(path, "r")
    except:
        return None
    else:
        return f

class PackageDB:
    DB_NAME = "data/pkgdb/freenas-db"
    __db_path = None
    __db_root = ""
    __conn = None
    __close = True

    def __init__(self, root="", create=True):
        if root is None:
            root = ""
        self.__db_root = root
        self.__db_path = self.__db_root + "/" + PackageDB.DB_NAME
        if os.path.exists(os.path.dirname(self.__db_path)) == False:
            if create is False:
                raise Exception("Cannot connect to database file {0}".format(self.__db_path))
            log.debug("Need to create %s", os.path.dirname(self.__db_path))
            os.makedirs(os.path.dirname(self.__db_path))

        if self._connectdb(returniferror=True, cursor=False) is None:
            raise Exception("Cannot connect to database file {0}".format(self.__db_path))

        cur = self.__conn.cursor()
        cur.execute(
            "CREATE TABLE IF NOT EXISTS packages(name text primary key, version text not null)"
        )
        cur.execute("CREATE TABLE IF NOT EXISTS scripts(package text not null, type text not null, script text not null)")
        cur.execute("""CREATE TABLE IF NOT EXISTS
        files(package text not null,
            path text primary key,
            kind text not null,
            checksum text,
            uid integer,
            gid integer,
            flags integer,
            mode integer)""")
        self._closedb()
        return

    def _connectdb(self, returniferror=False, cursor=False, isolation_level=None):
        import sqlite3
        if self.__conn is not None:
            if cursor:
                return self.__conn.cursor()
            return True
        try:
            conn = sqlite3.connect(self.__db_path, isolation_level=isolation_level)
        except Exception as err:
            log.error(
                "%s:  Cannot connect to database %s: %s",
                sys.argv[0],
                self.__db_path,
                str(err),
            )
            if returniferror:
                return None
            raise err

        conn.text_factory = str
        conn.row_factory = sqlite3.Row
        self.__conn = conn
        if cursor:
            return self.__conn.cursor()
        return True

    def _closedb(self):
        if self.__conn is not None:
            self.__conn.commit()
            self.__conn.close()
            self.__conn = None
        return

    def FindPackage(self, pkgName):
        self._connectdb()
        cur = self.__conn.cursor()
        cur.execute("SELECT name, version FROM packages WHERE name = ?", (pkgName, ))
        rv = cur.fetchone()
        self._closedb()
        if rv is None:
            return None
        return {rv["name"]: rv["version"]}

    def UpdatePackage(self, pkgName, curVers, newVers, scripts):
        cur = self.FindPackage(pkgName)
        if cur is None:
            raise Exception("Package {0} is not in system database, cannot update".format(pkgName))
        if cur[pkgName] != curVers:
            raise Exception(
                "Package {0} is at version {1}, not version {2} as requested by update".format(
                    pkgName, cur[pkgName], curVers
                )
            )

        if cur[pkgName] == newVers:
            log.warn(
                "Package %s version %s not changing, so not updating",
                pkgName,
                newVers,
            )
            return
        self._connectdb()
        cur = self.__conn.cursor()
        cur.execute("UPDATE packages SET version = ? WHERE name = ?", (newVers, pkgName))
        cur.execute("DELETE FROM scripts WHERE package = ?", (pkgName,))
        if scripts is not None:
            for scriptType in list(scripts.keys()):
                cur.execute("INSERT INTO scripts(package, type, script) VALUES(?, ?, ?)",
                            (pkgName, scriptType, scripts[scriptType]))

        self._closedb()

    def AddPackage(self, pkgName, vers, scripts):
        curVers = self.FindPackage(pkgName)
        if curVers is not None:
            raise Exception("Package %s is already in system database, cannot add" % pkgName)
        self._connectdb()
        cur = self.__conn.cursor()
        cur.execute("INSERT INTO packages VALUES(?, ?)", (pkgName, vers))
        if scripts is not None:
            for scriptType in list(scripts.keys()):
                cur.execute("INSERT INTO scripts(package, type, script) VALUES(?, ?, ?)",
                            (pkgName, scriptType, scripts[scriptType]))
        self._closedb()

    def FindScriptForPackage(self, pkgName, scriptType=None):
        cur = self._connectdb(cursor=True)
        if scriptType is None:
            cur.execute("SELECT type, script FROM scripts WHERE package = ?", (pkgName, ))
        else:
            cur.execute("SELECT type, script FROM scripts WHERE package = ? and type = ?",
                        (pkgName, scriptType))

        scripts = cur.fetchall()
        self._closedb()
        rv = {}
        for s in scripts:
            rv[s["type"]] = s["script"]

        return rv

    def FindFilesForPackage(self, pkgName=None):
        self._connectdb()
        cur = self.__conn.cursor()
        if pkgName is None:
            cur.execute("SELECT path, package, kind, checksum, uid, gid, flags, mode FROM files")
        else:
            cur.execute("SELECT path, package, kind, checksum, uid, gid, flags, mode FROM files WHERE package = ?", (pkgName,))

        files = cur.fetchall()
        self._closedb()
        rv = []
        for f in files:
            tmp = {}
            for k in list(f.keys()):
                tmp[k] = f[k]
            rv.append(tmp)
        return rv

    def FindFile(self, path):
        self._connectdb()
        cur = self.__conn.cursor()
        cur.execute("SELECT * FROM files WHERE path = ?", (path,))
        row = cur.fetchone()
        self._closedb()
        if row is None:
            return None
        rv = {}
        for k in list(row.keys()):
            rv[k] = row[k]
        return rv

    def AddFilesBulk(self, list):
        self._connectdb(isolation_level="DEFERRED")
        cur = self.__conn.cursor()
        stmt = "INSERT OR REPLACE INTO files(package, path, kind, checksum, uid, gid, flags, mode) VALUES(?, ?, ?, ?, ?, ?, ?, ?)"
        cur.executemany(stmt, list)
        self._closedb()

    def AddFile(self, pkgName, path, type, checksum="", uid=0, gid=0, flags=0, mode=0):
        update = False
        if self.FindFile(path) is not None:
            update = True
        self._connectdb()
        cur = self.__conn.cursor()
        if update:
            stmt = "UPDATE files SET package = ?, kind = ?, path = ?, checksum = ?, uid = ?, gid = ?, flags = ?, mode = ? WHERE path = ?"
            args = (pkgName, type, path, checksum, uid, gid, flags, mode, path)
        else:
            stmt = "INSERT INTO files(package, kind, path, checksum, uid, gid, flags, mode) VALUES(?, ?, ?, ?, ?, ?, ?, ?)"
            args = (pkgName, type, path, checksum, uid, gid, flags, mode)
        cur.execute(stmt, args)
        self._closedb()

    def RemoveFileEntry(self, path):
        if self.FindFile(path) is not None:
            self._connectdb()
            cur = self.__conn.cursor()
            cur.execute("DELETE FROM files WHERE path = ?", (path, ))
            self._closedb()
        return

    def RemovePackageFiles(self, pkgName):
        # Remove the files in a package.  This removes them from
        # both the filesystem and database.
        if self.FindPackage(pkgName) is None:
            log.warn("Package %s is not in database", pkgName)
            return False

        self._connectdb()
        cur = self.__conn.cursor()

        cur.execute("SELECT path FROM files WHERE package = ? AND kind <> ?", (pkgName, "dir"))
        rows = cur.fetchall()
        file_list = []
        for row in rows:
            path = row[0]
            full_path = self.__db_root + "/" + path
            if Installer.RemoveFile(full_path) == False:
                raise Exception("Cannot remove file %s" % path)
            file_list.append((path, ))
        cur.executemany("DELETE FROM files WHERE path = ?", file_list)
        cur.execute("VACUUM")
        self._closedb()
        return True

    def RemovePackageDirectories(self, pkgName, failDirectoryRemoval=False):
        # Remove the directories in a package.  This removes them from
        # both the filesystem and database.  If failDirectoryRemoval is True,
        # and a directory cannot be removed, return False.  Otherwise,
        # ignore that.

        if self.FindPackage(pkgName) is None:
            log.warn("Package %s is not in database", pkgName)
            return False

        self._connectdb()
        cur = self.__conn.cursor()

        dir_list = []
        # Sort the list of directories in descending order, so that
        # child directories get removed before their parents.
        cur.execute("SELECT path FROM files WHERE package = ? AND kind = ? ORDER BY path DESC", (pkgName, "dir"))
        rows = cur.fetchall()
        for row in rows:
            path = row[0]
            full_path = self.__db_root + "/" + path
            if Installer.RemoveDirectory(full_path) is False and failDirectoryRemoval is True:
                raise Exception("Cannot remove directory %s" % path)
            dir_list.append((path, ))
        cur.executemany("DELETE FROM files WHERE path = ?", dir_list)
        cur.execute("VACUUM")
        self._closedb()
        return True

    def RemovePackageScripts(self, pkgName):
        if self.FindPackage(pkgName) is None:
            log.warn(
                "Package %s is not in database, cannot remove scripts",
                pkgName,
            )
            return False

        cur = self._connectdb(cursor=True)
        cur.execute("DELETE FROM scripts WHERE package = ?", (pkgName, ))
        self._closedb()
        return True

    # This removes the contents of the given packages from both the filesystem
    # and the database.  It leaves the package itself in the database.
    def RemovePackageContents(self, pkgName, failDirectoryRemoval=False):
        if self.FindPackage(pkgName) is None:
            log.warn("Package %s is not in database", pkgName)
            return False

        if self.RemovePackageFiles(pkgName) == False:
            return False
        if self.RemovePackageDirectories(pkgName, failDirectoryRemoval) == False:
            return False
        if self.RemovePackageScripts(pkgName) == False:
            return False

        return True

    # Note that this just affects the database, it doesn't run any script.
    # That makes it the opposite of RemovePackageContents().
    def RemovePackage(self, pkgName):
        if self.FindPackage(pkgName) is not None:
            flist = self.FindFilesForPackage(pkgName)
            if len(flist) != 0:
                log.error(
                    "Can't remove package %s, it has %d files still",
                    pkgName,
                    len(flist),
                )
                raise Exception("Cannot remove package %s if it still has files" % pkgName)
            dlist = self.FindScriptForPackage(pkgName)
            if dlist is not None and len(dlist) != 0:
                log.error(
                    "Cannot remove package %s, it still has scripts",
                    pkgName,
                )
                raise Exception("Cannot remove package %s as it still has scripts" % pkgName)

        self._connectdb()
        cur = self.__conn.cursor()
        cur.execute("DELETE FROM packages WHERE name = ?", (pkgName, ))
        self._closedb()
        return


class UpdateServer(object):

    def __init__(self, name=None, url=None, master=None, signing=True):
        if name is None:
            raise ValueError("Cannot initialize UpdateServer with no name")
        else:
            self._name = name
        if url is None:
            raise ValueError("Cannot initialize UpdateServer with no URL")
        self._url = url
        self._master = master
        if master == url:
            self._master = None
        self._signature_required = signing

    def __repr__(self):
        return "UpdateServer(name={}, url={}, master={}, signing={})".format(
            self.name, self.url, self.master, self.signature_required)

    def __str__(self):
        return "<UpdateServe name={} url={} master={} signing={}>".format(
            self.name, self.url, self._master, self.signature_required)
    
    def __dict__(self):
        retval = { "name" : self.name, "url" : self.url, "signing" : self.signature_required }
        if self._master and self._master != self.url:
            retval["master"] = self.master
        return retval
    
    @property
    def master(self):
        if self._master:
            return self._master
        else:
            return self.url

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, name):
        if name is None:
            raise ValueError("Cannot set UpdateServer name to nothing")
        self._name = name

    @property
    def url(self):
        return self._url

    @url.setter
    def url(self, url):
        if url is None:
            raise ValueError("Cannot set UpdateServer URL to nothing!")
        self._url = url

    @property
    def signature_required(self):
        return self._signature_required

    @signature_required.setter
    def signature_required(self, sr):
        self._signature_required = sr

default_update_server = UpdateServer(name="default",
                                     url=UPDATE_SERVER,
                                     master=MASTER_UPDATE_SERVER,
                                     signing=True)


class Configuration(object):
    _root = ""
    _config_path = "/data/update.conf"
    _temp = "/tmp"
    _system_dataset = "/var/db/system"
    _package_dir = None

    _manifest = None

    def __init__(self, root=None, file=None):
        if root is not None:
            self._root = root
        if file is not None:
            self._config_path = file
        self._update_servers = { default_update_server.name : default_update_server }
        self._update_server_name = default_update_server.name
        self.LoadUpdateConfigurationFile(self._config_path)
        # Set _temp to the system pool, if it exists.
        if os.path.exists(self._system_dataset):
            self._temp = self._system_dataset

    def UpdateServerMaster(self):
        self.UpdateCache()
        return self._update_servers[self._update_server_name].master

    def UpdateServerURL(self):
        self.UpdateCache()
        return self._update_servers[self._update_server_name].url

    def UpdateServerName(self):
        self.UpdateCache()
        return self._update_servers[self._update_server_name].name

    def UpdateServerSigned(self):
        self.UpdateCache()
        return self._update_servers[self._update_server_name].signature_required

    def ListUpdateServers(self):
        self.UpdateCache()
        return list(self._update_servers.keys())
    
    def SetUpdateServer(self, name=default_update_server.name, save=True):
        if name not in self._update_servers:
            raise LookupError("Update server {} not found".format(name))
        self._update_server_name = name
        if save:
            self.StoreUpdateConfigurationFile(self._config_path)
        
    def AddUpdateServer(self, server, save=True):
        if server is None:
            raise ValueError("Cannot set an empty update server")
        if server.name == default_update_server.name:
            return
        self._update_servers[server.name] = server
        if save:
            self.StoreUpdateConfigurationFile(self._config_path)
        
    def RemoveUpdateServer(self, name, save=True):
        if name is None:
            raise ValueError("Cannot remove None from update server list")
        if name == default_update_server.name:
            # Can't delete it, but we'll ignore it for now
            return
        if name not in self._update_servers:
            raise LookupError("Cannot remove update server {} because it doesn't exist".format(name))
        self._update_servers.pop(name, None)
        if self._update_server_name == name:
            self._update_server_name = default_update_server.name
        if save:
            self.StoreUpdateConfigurationFile(self._config_path)
        
    def TryGetNetworkFile(self, file=None, url=None, handler=None,
                          pathname=None, reason=None, intr_ok=False,
                          ignore_space=False):
        # Lazy import requests to not require it on install
        import requests
        import urllib3.exceptions

        AVATAR_VERSION = "X-%s-Manifest-Version" % Avatar()
        current_sequence = "unknown"
        current_train = None
        current_version = None
        host_id = None
        if file and url:
            log.debug("Cannot specify both file and url for TryGetNetworkFile")
            raise Exception("Bad use of TryGetNetworkFile")
        if (not file) and (not url):
            log.debug("Must specify at file xor url for TryGetNetworkFile")
            raise Exception("Bad use of TryGetNetworkFile again")

        if file:
            # If we're looking for a file in general, look in the update server and the master.
            file_url = ["%s/%s" % (self.UpdateServerURL(), file)]
            if self.UpdateServerURL() != self.UpdateServerMaster():
                file_url.append("%s/%s" % (self.UpdateServerMaster(), file))
        elif url:
            file_url = [url]
        log.debug("TryGetNetworkFile(%s)" % file_url)
        temp_mani = self.SystemManifest()
        if temp_mani:
            current_sequence = temp_mani.Sequence()
            current_version = temp_mani.Version()
            current_train = temp_mani.Train()
        try:
            from bsd.sysctl import sysctlbyname
            host_id = sysctlbyname("kern.hostuuid").strip('\x00')
        except:
            host_id = None

        license_data = None
        try:
            from freenasUI.support.utils import LICENSE_FILE
            with open(LICENSE_FILE, "r") as f:
                license_data = f.read().rstrip()
                # Make sure license data is a valid header (and base64 string)
                # See #21179
                if not re.search(r'^[a-z0-9\+\/]+[=]*$', license_data, re.I):
                    license_data = None
        except:
            pass

        read = 0
        retval = None
        try:
            if pathname:
                if intr_ok:
                    try:
                        retval = open(pathname, "r+b")
                        read = os.fstat(retval.fileno()).st_size
                        retval.seek(read)
                    except:
                        pass
                if retval is None:
                    retval = open(pathname, "w+b")
            else:
                retval = tempfile.TemporaryFile(dir=self._temp)

            if read > 0:
                log.debug("File already exists, using a starting size of %d" % read)

            furl = None
            for url in file_url:
                url_exc = None
                try:
                    header_dict = {
                        "X-iXSystems-Project" : Avatar(),
                        "X-iXSystems-Version" : current_sequence,
                        "User-Agent" : "%s=%s" % (AVATAR_VERSION, current_version)
                    }
                    if current_version:
                        header_dict["X-iXSystems-Version-Name"] = current_version
                    if current_train:
                        header_dict["X-iXSystems-Train"] = current_train
                    if host_id:
                        header_dict["X-iXSystems-HostID"] = host_id
                    if reason:
                        header_dict["X-iXSystems-Reason"] = reason
                    if license_data:
                        header_dict["X-iXSystems-License"] = license_data

                    # Allow restarting
                    if intr_ok:
                        header_dict["Range"] = "bytes=%d-" % read

                    furl = requests.get(url, timeout=10, verify=DEFAULT_CA_FILE,
                                       stream=True, headers=header_dict)
                    furl.raise_for_status()
                except requests.exceptions.HTTPError as error:
                    if error.response.status_code == HTTP_RANGE.value:
                        # We've reached the end of the file already
                        # Can I get this incorrectly from any other server?
                        # Do I need to do something different for the progress handler?
                        retval.seek(0)
                        return retval
                    elif error.response.status_code == HTTP_NOT_FOUND.value:
                        # The requested file is not found on this server.
                        url_exc = Exceptions.UpdateNetworkFileNotFoundException("Requested file %s not found" % (file if file else url))
                        log.error("Error 404: %s" % str(url_exc))
                    else:
                        log.error("Got http error %s" % str(error))
                        url_exc = Exceptions.UpdateNetworkServerException("Unable to load from url %s: %d" % (url, error.response.status_code))
                        url_exc = error
                except requests.exceptions.ConnectionError as e:
                    log.error("Unable to connect to url %s: %s" % (url, str(e)))
                    url_exc = Exceptions.UpdateNetworkConnectionException("Uable to connect to url %s" % url)
                except BaseException as e:
                    log.error("Unable to load %s: %s", url, str(e))
                    url_exc = e

                if furl:
                    break

            # The loop above should leave url_exc set to None if the file
            # was grabbed.
            if url_exc:
                if furl:
                    furl.close()
                    furl = None
                if retval:
                    retval.close()
                log.error("Unable to load %s: %s", file_url, str(url_exc))
                raise url_exc

            # This _shouldn't_ be doable, but I'm checking just in case.
            if furl is None:
                log.error("Unable to load %s", file_url)
                if retval:
                    retval.close()
                return None

            try:
                totalsize = read + int(furl.headers['Content-Length'].strip())
            except:
                totalsize = None

            if totalsize and pathname and ignore_space is False:
                space_needed = totalsize - read

                if CheckFreeSpace(path=pathname, required=space_needed) is False:
                    # Hm, we don't distinguish between out of space, and zfs performance check
                    raise Exceptions.UpdateInsufficientSpace("Insufficient space")

            chunk_size = 64 * 1024
            mbyte = 1024 * 1024
            lastpercent = percent = 0
            lasttime = time.time()
            try:
                while True:
                    data = furl.raw.read(chunk_size)
                    tmptime = time.time()
                    if tmptime - lasttime > 0:
                        downrate = int(chunk_size / (tmptime - lasttime))
                    else:
                        downrate = chunk_size
                    lasttime = tmptime
                    if not data:
                        log.debug("TryGetNetworkFile(%s):  Read %d bytes total" % (file_url, read))
                        break
                    read += len(data)
                    if ((read % mbyte) == 0):
                        log.debug("TryGetNetworkFile(%s):  Read %d bytes" % (file_url, read))

                    if handler and totalsize:
                        percent = int((float(read) / float(totalsize)) * 100.0)
                        if percent != lastpercent:
                            handler(
                                'network',
                                url,
                                size=totalsize,
                                progress=percent,
                                download_rate=downrate,
                            )
                        lastpercent = percent
                    retval.write(data)
            except Exception as e:
                log.debug("Got exception %s" % str(e), exc_info=True)
                if intr_ok is False and pathname:
                    os.unlink(pathname)
                raise e
            retval.seek(0)
        except:
            if retval:
                retval.close()
            raise
        return retval

    # Load the list of currently-watched trains.
    # The file is a JSON file.
    # This sets self._trains as a dictionary of
    # Train objects (key being the train name).
    def LoadTrainsConfig(self, updatecheck=False):
        import json
        self._trains = {}
        if self._temp:
            train_path = self._temp + "/Trains.json"
            try:
                with open(train_path, "r") as f:
                    trains = json.load(f)
                for train_name in list(trains.keys()):
                    temp = Train.Train(train_name)
                    if TRAIN_DESC_KEY in trains[train_name]:
                        temp.SetDescription(trains[train_name][TRAIN_DESC_KEY])
                    if TRAIN_SEQ_KEY in trains[train_name]:
                        temp.SetLastSequence(trains[train_name][TRAIN_SEQ_KEY])
                    if TRAIN_CHECKED_KEY in trains[train_name]:
                        temp.SetLastCheckedTime(trains[train_name][TRAIN_CHECKED_KEY])
                    self._trains[train_name] = temp
            except:
                pass
        sys_mani = self.SystemManifest()
        if sys_mani.Train() not in self._trains:
            temp = Train.Train(sys_mani.Train(), "Installed OS", sys_mani.Sequence())
            self._trains[temp.Name()] = temp
        if updatecheck:
            for train in self._trains:
                new_man = self.FindLatestManifest(train.Name())
                if new_man:
                    if new_man.Sequence() != train.LastSequence():
                        # We have an update
                        train.SetLastSequence(new_man.Sequence())
                        train.SetLastCheckedTime()
                        train.SetNotes(new_man.Notes())
                        train.SetNotice(new_man.Notice())
                        train.SetUpdate(True)
        return

    # Save the list of currently-watched trains.
    def SaveTrainsConfig(self):
        import json
        sys_mani = self.SystemManifest()
        current_train = sys_mani.Train()
        if self._trains is None:
            self._trains = {}
        if current_train not in self._trains:
            self._trains[current_train] = Train.Train(current_train, "Installed OS", sys_mani.Sequence())
        if self._temp:
            obj = {}
            for train_name in list(self._trains.keys()):
                train = self._trains[train_name]
                temp = {}
                if train.Description():
                    temp[TRAIN_DESC_KEY] = train.Description()
                if train.LastSequence():
                    temp[TRAIN_SEQ_KEY] = train.LastSequence()
                if train.LastCheckedTime():
                    temp[TRAIN_CHECKED_KEY] = train.LastCheckedTime()
                obj[train_name] = temp
            train_path = self._temp + "/Trains.json"
            try:
                with open(train_path, "w") as f:
                    json.dump(obj, f, sort_keys=True,
                              indent=4, separators=(',', ': '))
            except OSError as e:
                log.error("Could not write out trains:  %s" % str(e))
        return

    def SystemManifest(self):
        if self._manifest is None:
            self._manifest = Manifest.Manifest(configuration = self)
            try:
                self._manifest.LoadPath(self._root + Manifest.SYSTEM_MANIFEST_FILE)
            except:
                self._manifest = None
        return self._manifest

    def PackageDB(self, root=None, create=True):
        if root is None:
            root = self._root
        return PackageDB(root, create)

    def StoreUpdateConfigurationFile(self, path):
        cfp = configparser.ConfigParser()
        if os.path.islink(self._root + path):
            os.remove(self._root + path)

        config_file = open(self._root + path, "w")

        if self._update_server_name != default_update_server.name:
            # We are using a different one
            cfp.add_section(CONFIG_DEFAULT)
            cfp.set(CONFIG_DEFAULT, CONFIG_SERVER, self._update_server_name)
        for name, server in self._update_servers.items():
            if name == default_update_server.name:
                # We don't write this one out
                continue
            cfp.add_section(name)
            for k, v in server.__dict__().items():
                if v is not None:
                    cfp.set(name, k, str(v))
        cfp.write(config_file)
        config_file.close()
        return
    
    def UpdateCache(self):
        """
        Update the cached fields if necessary.  At this
        point, this only means the update configuration file.
        """
        self.LoadUpdateConfigurationFile(self._config_path)
        
    def LoadUpdateConfigurationFile(self, path):
        cfp = None
        try:
            with open(self._root + path, "r") as f:
                mtime = os.stat(f.name).st_mtime
                try:
                    if mtime <= self._upd_conf_mtime:
                        return
                except:
                    self._upd_conf_mtime = mtime
                cfp = configparser.ConfigParser()
                if six.PY2:
                    cfp.readfp(f)
                elif six.PY3:
                    cfp.read_file(f)
        except:
            # If we don't have an update configuration file,
            # we need to use the defaults, no matter what
            # we've used before
            self._update_server_name = default_update_server.name
            return

        if cfp is None:
            return

        upd = None
        for section in cfp.sections():
            if section == CONFIG_DEFAULT:
                if cfp.has_option(CONFIG_DEFAULT, CONFIG_SERVER):
                    self._update_server_name = cfp.get(CONFIG_DEFAULT, CONFIG_SERVER)
            else:
                if cfp.has_option(section, UPDATE_SERVER_NAME_KEY) and \
                   cfp.has_option(section, UPDATE_SERVER_URL_KEY):
                    # This is an update server section
                    n = cfp.get(section, UPDATE_SERVER_NAME_KEY)
                    u = cfp.get(section, UPDATE_SERVER_URL_KEY)
                    s = cfp.getboolean(section, UPDATE_SERVER_SIGNED_KEY) \
                        if cfp.has_option(section, UPDATE_SERVER_SIGNED_KEY) else True
                    m = cfp.get(section, UPDATE_SERVER_MASTER_KEY) \
                        if cfp.has_option(section, UPDATE_SERVER_MASTER_KEY) else None
                    try:
                        update_server = UpdateServer(name=n, url=u, signing=s, master=m)
                        self._update_servers[section] = update_server
                    except:
                        log.error("Cannot set update server to %s, using default", n)
        # End for loop here
        if self._update_server_name not in self._update_servers:
            log.error("Selected update server is not in list of update servers, using default")
            self._update_server_name = default_update_server.name
        return

    def SetPackageDir(self, loc):
        self._package_dir = loc
        return

    def AddSearchLocation(self, loc, insert=False):
        raise Exception("Deprecated method")
        if self._search is None:
            self._search = []
        if insert is True:
            self._search.insert(0, loc)
        else:
            self._search.append(loc)
        return

    def SetSearchLocations(self, list):
        raise Exception("Deprecated method")
        self._search = list
        return

    def AddTrain(self, train):
        self._trains.append(train)
        return

    def CurrentTrain(self):
        """
        Returns the name of the train of the current
        system.  It may return None, but that's for edge cases
        generally related to installation and build environments.
        """
        sys_mani = self.SystemManifest()
        if sys_mani:
            if sys_mani.NewTrain():
                return sys_mani.NewTrain()
            return sys_mani.Train()
        return None

    def AvailableTrains(self):
        """
        Returns the set of available trains from
        the upgrade server.  The return value is
        a dictionary, keyed by the train name, and
        value being the description.
        The list of trains is on the upgrade server,
        with the name "trains.txt".  Or whatever
        I decide it should be called.
        """
        rv = {}
        fileref = self.TryGetNetworkFile(file=TRAIN_FILE, reason="FetchTrains")

        if fileref is None:
            return None

        for line in fileref:
            import re
            line = line.decode('utf8').rstrip()
            # Ignore comments
            if line.startswith("#"):
                continue
            # Input is <name><white_space><description>
            m = re.search("(\S+)\s+(.*)$", line)
            if m is None or m.lastindex is None:
                log.debug("Input line `%s' is unparsable" % line)
                continue
            rv[m.group(1)] = m.group(2)

        return rv if len(rv) > 0 else None

    def WatchedTrains(self):
        if self._trains is None:
            self._trains = self.LoadTrainsConfig()
        return self._trains

    def WatchTrain(self, train, watch=True):
        """
        Add a train to the local set to be watched.
        A watched train is checked for updates.
        If the train is already watched, this does nothing.
        train is a Train object.
        If stop is True, then this is used to stop watching
        this particular train.
        """
        if self._trains is None:
            self._trains = {}
        if watch:
            if train.Name() not in self._trains:
                self._trains[train.Name()] = train
        else:
            if train.Name() in self._trains:
                self._trains.pop(train.Name())
        return

    def SetTrains(self, tlist):
        self._trains = tlist
        return

    def TemporaryDirectory(self):
        return self._temp

    def SetTemporaryDirectory(self, path):
        if path:
            self._temp = path
        return

    def CreateTemporaryFile(self):
        return tempfile.TemporaryFile(dir=self._temp)

    def PackagePath(self, pkg):
        if self._package_dir:
            return "%s/%s" % (self._package_dir, pkg.FileName())
        else:
            return "%s/Packages/%s" % (self.UpdateServerURL(), pkg.FileName())

    def PackageUpdatePath(self, pkg, old_version):
        # Do we need this?  If we're given a package directory,
        # then we won't have updates.
        if self._package_dir:
            return "%s/%s" % (self._package_dir, pkg.FileName(old_version))
        else:
            return "%s/Packages/%s" % (self.UpdateServerURL(), pkg.FileName(old_version))

    def GetManifest(self, train=None, sequence=None, handler=None):
        """
        GetManifest:  fetch, over the network, the requested
        manifest file.  If train isn't specified, it'll use
        the current system; if sequence isn't specified, it'll
        use LATEST.
        Returns either a manifest object, or None.
        """
        # Get the specified manifeset.
        # If train is None, then we use the current train;
        # if sequence is None, then we get LATEST.
        sys_mani = self.SystemManifest()
        if sys_mani is None:
            raise Exceptions.ConfigurationInvalidException
        if train is None:
            train = sys_mani.Train()
        if sequence is None:
            ManifestFile = "/%s/LATEST" % train
        else:
            # This needs to change for TrueNAS, doesn't it?
            ManifestFile = "%s/%s-%s" % (Avatar(), train, sequence)

        file_ref = self.TryGetNetworkFile(url="%s/%s" % (self.UpdateServerMaster(), ManifestFile),
                                          handler=handler,
                                          reason="GetManifest")
        return file_ref

    def FindLatestManifest(self, train=None, require_signature=False):
        # Gets <UPDATE_SERVER>/<train>/LATEST
        # Returns a manifest, or None.
        rv = None
        temp_mani = self.SystemManifest()

        if train is None:
            if temp_mani is None:
                # I give up
                raise Exceptions.ConfigurationInvalidException
            if temp_mani.NewTrain():
                # If we're redirected to a new train, use that.
                train = temp_mani.NewTrain()
            else:
                train = temp_mani.Train()

        mani_file = self.TryGetNetworkFile(url="%s/%s/LATEST" % (self.UpdateServerMaster(), train),
                                      reason="GetLatestManifest",
                                      )
        if mani_file is None:
            log.debug("Could not get latest manifest file for train %s" % train)
        else:
            rv = Manifest.Manifest(self, require_signature=require_signature)
            rv.LoadFile(mani_file)
            mani_file.close()
        return rv

    def CurrentPackageVersion(self, pkgName):
        try:
            pkgdb = self.PackageDB(create=False)
            if pkgdb:
                pkgInfo = pkgdb.FindPackage(pkgName)
                if pkgInfo:
                    return pkgInfo[pkgName]
        except:
            pass
        return None

    def GetChangeLog(self, train, save_dir=None, handler=None):
        # Look for the changelog file for the specific train, and attempt to
        # download it.  If save_dir is set, save it as save_dir/ChangeLog.txt
        # Returns a file for the ChangeLog, or None if it can't be found.
        changelog_url = "%s/ChangeLog.txt" % train
        if save_dir:
            save_path = "%s/ChangeLog.txt" % save_dir
        else:
            save_path = None
        try:
            file = self.TryGetNetworkFile(
                file=changelog_url,
                handler=handler,
                pathname=save_path,
                reason="GetChangeLog",
            )
            return file
        except:
            log.debug("Could not get ChangeLog.txt, ignoring")
            return None

    def FindPackageFile(self, package, upgrade_from=None, handler=None,
                        save_dir=None, pkg_type=None, ignore_space=False):
        # Given a package, and optionally a version to upgrade from, find
        # the package file for it.  Returns a file-like
        # object for the package file.
        # If the package object has a checksum set, it
        # attempts to verify the checksum; if it doesn't match,
        # it goes onto the next one.
        # If upgrade_from is set, it tries to find delta packages
        # first, and will verify the checksum for that.  If the
        # package does not have an upgrade field set, or it does
        # but there's no checksum, then we are probably creating
        # the manifest file, so we won't do the checksum verification --
        # we'll only go by name.
        # If it can't find one, it returns None

        # We have at least one, and at most two, files
        # to look for.
        # The first file is the full package.

        # Leave this local import here as otherwise it causes circular import issues
        from .Update import PkgFileDeltaOnly, PkgFileFullOnly
        package_files = []
        if pkg_type is not PkgFileDeltaOnly:
            package_files.append({"Filename": package.FileName(), "Checksum": package.Checksum()})
        # The next one is the delta package, if it exists.
        # For that, we look through package.Updates(), looking for one that
        # has the same version as what is currently installed.
        # So first we have to get the current version.
        if pkg_type is not PkgFileFullOnly:
            try:
                pkgdb = self.PackageDB(create=False)
                if pkgdb:
                    pkgInfo = pkgdb.FindPackage(package.Name())
                    if pkgInfo:
                        curVers = pkgInfo[package.Name()]
                        if curVers and curVers != package.Version():
                            upgrade = package.Update(curVers)
                            if upgrade:
                                tdict = {
                                    "Filename": package.FileName(curVers),
                                    "Checksum": None,
                                    "Reboot": upgrade.RequiresReboot(),
                                    "Delta": True,
                                }
                                if upgrade.Checksum():
                                    tdict[Package.CHECKSUM_KEY] = upgrade.Checksum()
                                if upgrade.Size():
                                    tdict[Package.SIZE_KEY] = upgrade.Size()
                                package_files.append(tdict)
            except:
                # No update packge that matches.
                pass

        # At this point, package_files now has at least one element.
        # We want to search in this order:
        # * Local full copy
        # * Local delta copy
        # * Network delta copy
        # * Network full copy

        # We want to look for each one in _package_dir and off the network.
        # If we find it, and the checksum matches, we're good to go.
        # If not, we have to grab it off the network and use that.  We can't
        # check that checksum until we get it.
        pkg_exception = None
        for search_attempt in package_files:
            # First try the local copy.
            log.debug("Searching for %s" % search_attempt["Filename"])
            try:
                if self._package_dir:
                    p = "{0}/{1}".format(self._package_dir, search_attempt["Filename"])
                    if os.path.exists(p):
                        file = open(p, 'rb')
                        log.debug("Found package file %s" % p)
                        if search_attempt["Checksum"]:
                            h = ChecksumFile(file)
                            if h == search_attempt["Checksum"]:
                                return file
                            else:
                                pkg_exception = Exceptions.ChecksumFailException("%{0} has invalid checksum".format(search_attempt["Filename"]))
                        else:
                            # No checksum for the file, so we'll just go with it.
                            return file
            except:
                pass

        for search_attempt in reversed(package_files):
            # Next we try to get it from the network.
            pFile = "Packages/%s" % search_attempt["Filename"]
            save_name = None
            if save_dir:
                save_name = save_dir + "/" + search_attempt["Filename"]

            try:
                file = None
                file = self.TryGetNetworkFile(
                    file=pFile,
                    handler=handler,
                    pathname=save_name,
                    reason="DownloadPackageFile",
                    intr_ok=True,
                    ignore_space=ignore_space
                )
            except BaseException as e:
                log.debug("Trying to get %s, got exception %s, continuing" % (pFile, str(e)))
                continue

            if file:
                if search_attempt["Checksum"]:
                    h = ChecksumFile(file)
                    if h == search_attempt["Checksum"]:
                        return file
                    else:
                        # For an interrupted download of a package file,
                        # this won't be reached due to an exception.
                        log.debug("Checksum doesn't match, removing file")
                        if save_name:
                            os.unlink(save_name)
                        pkg_exception = Exceptions.ChecksumFailException("%{0} has invalid checksum".format(pFile))
                else:
                    # No checksum for the file, so we just go with it
                    return file

        if file:
            file.close()
        if pkg_exception:
            raise pkg_exception
        raise Exceptions.UpdatePackageNotFound(package.Name())

_system_config = None
def SystemConfiguration():
    global _system_config
    if _system_config is None:
        _system_config = Configuration()
    return _system_config

def is_ignore_path(path):
    for i in VERIFY_SKIP_PATHS:
        tlen = len(i)
        if path[:tlen] == i:
            return True
    return False


def get_ftype_and_perm(mode):
    """
    Returns a tuple of whether the file is: file(regular file)/dir/slink
    /char. spec/block spec/pipe/socket and the permission bits of the file.
    If it does not match any of the cases below (it will return "unknown" twice)
    """

    if S_ISREG(mode):
        return "file", S_IMODE(mode)
    if S_ISDIR(mode):
        return "dir", S_IMODE(mode)
    if S_ISLNK(mode):
        return "slink", S_IMODE(mode)
    if S_ISCHR(mode):
        return "character special", S_IMODE(mode)
    if S_ISBLK(mode):
        return "block special", S_IMODE(mode)
    if S_ISFIFO(mode):
        return "pipe", S_IMODE(mode)
    if S_ISSOCK(mode):
        return "socket", S_IMODE(mode)
    return "unknown", "unknown"


def check_ftype(objs):
    """
    Checks the filetype, permissions and uid,gid of the
    pkgdg object(objs) sent to it. Returns two dicts: ed and pd
    (the error_dict with a descriptive explanantion of the problem
    if present, none otherwise, the perm_dict with a description of
    the incoorect perms if present, none otherwise
    """

    ed = None
    pd = None
    lst_var = os.lstat(objs["path"])
    ftype, perm = get_ftype_and_perm(lst_var.st_mode)
    if ftype != objs["kind"]:
        ed = dict([
            ('path', objs["path"]),
            ('problem', 'Expected {0}, Got {1}'.format(objs["kind"], ftype)),
            ('pkgdb_entry', objs)
        ])
    pdtmp = ''
    if perm != objs["mode"]:
        pdtmp += "\nExpected MODE: {0}, Got: {1}".format(oct(objs["mode"]), oct(perm))
    if lst_var.st_uid != objs["uid"]:
        pdtmp += "\nExpected UID: {0}, Got: {1}".format(objs["uid"], lst_var.st_uid)
    if lst_var.st_gid != objs["gid"]:
        pdtmp += "\nExpected GID: {0}, Got: {1}".format(objs["gid"], lst_var.st_gid)
    if pdtmp and not objs["path"].endswith(".pyc"):
        pd = dict([
            ('path', objs["path"]),
            ('problem', pdtmp[1:]),
            ('pkgdb_entry', objs)
        ])
    return ed, pd


def do_verify(verify_handler=None):
    """
    A function that goes through the provided pkgdb filelist and verifies it with
    the current root filesystem.
    """

    error_flag = False
    error_list = dict([
        ('checksum', []),
        ('wrongtype', []),
        ('notfound', [])
    ])
    warn_flag = False
    warn_list = []
    i = 0  # counter for progress indication in the UI

    pkgdb = PackageDB(create=False)
    if pkgdb is None:
        raise IOError("Cannot get pkgdb connection")
    filelist = pkgdb.FindFilesForPackage()
    total_files = len(filelist)

    for objs in filelist:
        i = i+1
        if verify_handler is not None:
            verify_handler(i, total_files, objs["path"])
        tmp = b''  # Just a temp. variable to store the text to be hashed
        if is_ignore_path(objs["path"]):
            continue
        if not os.path.lexists(objs["path"]):
            # This basically just checks if the file/slink/dir exists or not.
            # Note: not using os.path.exists(path) here as that returns false
            # even if its a broken symlink and that is a differret problem
            # and will be caught in one of the if conds below.
            # For more information: https://docs.python.org/2/library/os.path.html
            error_flag = True
            error_list['notfound'].append(dict([
                ('path', objs["path"]),
                ('problem', 'path does not exsist'),
                ('pkgdb_entry', objs)
            ]))
            continue

        ed, pd = check_ftype(objs)
        if ed:
            error_flag = True
            error_list['wrongtype'].append(ed)
        if pd:
            warn_flag = True
            warn_list.append(pd)

        if objs["kind"] == "slink":
            tmp = os.readlink(objs["path"]).encode('utf8')
            if tmp.startswith(b'/'):
                tmp = tmp[1:]

        if objs["kind"] == "file":
            if objs["path"].endswith(".pyc"):
                continue
            with open(objs["path"], 'rb') as f:
                tmp = f.read()

        # Do this last (as it needs to be done for all, but dirs, as dirs have no checksum d'oh!)
        if (
            objs["kind"] != 'dir' and
            objs["checksum"] and
            objs["checksum"] != "-" and
            hashlib.sha256(tmp).hexdigest() != objs["checksum"]
           ):
            error_flag = True
            error_list['checksum'].append(dict([
                ('path', objs["path"]),
                ('problem', 'checksum does not match'),
                ('pkgdb_entry', objs)
            ]))
    return error_flag, error_list, warn_flag, warn_list
