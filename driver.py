#!/home/ismaeel/venv/bin/python3

import os
import datetime
import sys
import stat
import errno
import time
import argparse
import logging
import threading
import yaml
import jsonpickle
from collections import defaultdict
import fuse
from fuse import Fuse
from pyicloud import PyiCloudService
from io import BytesIO

# Fix for __version__ check in fuse.py
if not hasattr(fuse, '__version__'):
    fuse.__version__ = "0.2"

# Ensure all operations are defined
fuse.fuse_python_api = (0, 2)

class Stat(fuse.Stat):
    def __init__(self):
        self.st_mode = 0
        self.st_ino = 0
        self.st_dev = 0
        self.st_nlink = 0
        self.st_uid = 0
        self.st_gid = 0
        self.st_size = 0
        self.st_atime = 0
        self.st_mtime = 0
        self.st_ctime = 0

class ICloudFS(fuse.Fuse):
    """
    A FUSE driver for mounting iCloud as a filesystem using Fuse2 with fuse-python.
    """

    def __init__(self, *args, **kw):
        """Initialize the filesystem"""
        super(ICloudFS, self).__init__(*args, **kw)
        
        self.logger = logging.getLogger('icloud-fuse')
        self.logger.info("Initializing iCloud FUSE filesystem")
        
        # These will be set in the main function after parsing config
        self.username = None
        self.password = None
        self.cache_dir = None
        self.api = None
        
        # Content cache: path -> (content, timestamp)
        self.content_cache = {}
        
        # File attributes cache: path -> (attrs, timestamp)
        self.attr_cache = {}
        
        # Directory listing cache: path -> (entries, timestamp)
        self.dir_cache = {}
        
        # Cache expiration time (seconds)
        self.cache_timeout = 300  # 5 minutes
        
        # Lock for cache access
        self.cache_lock = threading.RLock()
        
        self.fd = 0
        self.fd_lock = threading.Lock()
        self.fd_map = {}  # Maps file descriptors to file paths
        
    def init_icloud(self, username, password, cache_dir):
        """Initialize iCloud connection"""
        self.username = username
        self.password = password
        self.cache_dir = cache_dir
        
        os.makedirs(self.cache_dir, exist_ok=True)
        
        try:
            self.api = PyiCloudService(username, password)
            if self.api.requires_2fa:
                print("Two-factor authentication required.")
                code = input("Enter the verification code: ")
                result = self.api.validate_2fa_code(code)
                print("Result: %s" % result)
                
                if not result:
                    print("Failed to verify 2FA code")
                    sys.exit(1)
                    
            if self.api.requires_2fa:
                print("Two-factor authentication still required. Exiting.")
                sys.exit(1)
        except Exception as e:
            self.logger.error(f"Failed to connect to iCloud: {str(e)}")
            raise

    def _get_next_fd(self):
        """Get the next available file descriptor"""
        with self.fd_lock:
            fd = self.fd
            self.fd += 1
            return fd
            
    def _get_drive_item(self, path):
        """Get an item from iCloud Drive by path"""
        if path == '/' or path == '':
            return self.api.drive
            
        self.logger.debug("Getting drive item from path: " + str(path))
        components = path.strip('/').split('/')
        self.logger.debug("Components: " + str(components))
        
        try:
            # for component in components:
            #     if not component:
            #         continue
            #     found = False
            #     for item in current.dir():
            #         self.logger.debug("Checking item: " + str(item) + " for " + component)
            #         if str(item) == component:
            #             current = item
            #             found = True
            #             break
            #     if not found:
            #         return None
            # self.logger.debug("Found!: " + str(current))
            # return current
            for component in components:
                ls = self.api.drive.dir()
                for item in ls:
                    if item == component:
                        item_in_drive = self.api.drive[item]
                        # example in sample-item.py
                        #self.logger.debug("get_drive_item: " + jsonpickle.encode(item_in_drive))
                        self.logger.debug("Successfully found and returning drive item")
                        return item_in_drive
            return None
        except Exception as e:
            self.logger.error(f"Error finding drive item at {path}: {str(e)}")
            return None

    def _get_path_type(self, path):
        """Determine if a path is a file or directory"""
        item = self._get_drive_item(path)
        # self.logger.debug("Getting path type from item obj: " + jsonpickle.encode(item))
        if item is None:
            return None
        if 'type' in item.data:
            self.logger.debug("Item type: " + item.data['type'])
            return item.data['type']
        # Default to folder if type attribute is missing
        defaulted_type = 'FOLDER' if hasattr(item, 'dir') else 'FILE'
        self.logger.debug("Defaulted type: " + defaulted_type)
        return defaulted_type

    def getattr(self, path):
        """Get file attributes"""
        with self.cache_lock:
            # Check cache first
            if path in self.attr_cache:
                attrs, timestamp = self.attr_cache[path]
                if time.time() - timestamp < self.cache_timeout:
                    self.logger.debug("Attributes for path " + str(path) + ": " + str(attrs))
                    return attrs
        
        self.logger.debug(f"getattr: {path}")
        now = int(time.time())
        
        attrs = Stat()

        if path == '/':
            attrs.st_mode = stat.S_IFDIR | 0o755
            attrs.st_nlink = 2
            attrs.st_size = 0
            attrs.st_ctime = now
            attrs.st_mtime = now
            attrs.st_atime = now
            attrs.st_uid = os.getuid()
            attrs.st_gid = os.getgid()

            with self.cache_lock:
                self.attr_cache[path] = (attrs, now)
            self.logger.debug("Path was /")
            return attrs
            
        item = self._get_drive_item(path)
        if item is None:
            self.logger.error("Item not found")
            return -errno.ENOENT
            
        item_type = self._get_path_type(path)
        
        if item_type == 'FOLDER':
            self.logger.debug("Item is folder")
            attrs.st_mode = stat.S_IFDIR | 0o755
            attrs.st_nlink = 3
            attrs.st_size = 0
            attrs.st_ctime = now
            attrs.st_mtime = now
            attrs.st_atime = now
            attrs.st_uid = os.getuid()
            attrs.st_gid = os.getgid()
        else:
            self.logger.debug("Item is not a folder")
            size = item.data['size'] if 'size' in item.data else 4096 
            modified = item.data['dateModified'] if 'dateModified' in item.data else None
            if modified:
                parsing = datetime.datetime.strptime(modified, "%Y-%m-%dT%H:%M:%SZ")
                mtime = time.mktime(parsing.timetuple())
            else:
                mtime = now
            mtime = int(mtime)
            
            attrs.st_mode = stat.S_IFREG | 0o644
            attrs.st_nlink = 1
            attrs.st_size = size
            attrs.st_ctime = mtime
            attrs.st_mtime = mtime
            attrs.st_atime = now
            attrs.st_uid = os.getuid()
            attrs.st_gid = os.getgid()
            
        with self.cache_lock:
            self.attr_cache[path] = (attrs, now)
        
        self.logger.debug("Final attributes: " + str(attrs))
        return attrs

    def readdir(self, path, offset):
        """Read directory entries"""
        self.logger.debug(f"readdir: {path}, offset: {offset}")
        
        entries = ['.', '..']
        with self.cache_lock:
            # Check cache first
            if path in self.dir_cache:
                self.logger.debug("readdir: in cache")
                entries_stored, timestamp = self.dir_cache[path]
                if time.time() - timestamp < self.cache_timeout:
                    entries = entries_stored
        
        if len(entries) <= 2:  # Not in cache or cache has just . and ..
            self.logger.debug("readdir: not in cache")
            try:
                item = self._get_drive_item(path)
                if item is None:
                    return -errno.ENOENT
                   
                if hasattr(item, 'dir'):
                    # List directory contents
                    for child in item.dir():
                        entries.append(child)
                else:
                    return -errno.ENOTDIR
                    
                with self.cache_lock:
                    self.dir_cache[path] = (entries, time.time())
            except Exception as e:
                self.logger.error(f"Error listing directory {path}: {str(e)}")
                return -errno.EIO
                
        # Yield each entry
        for e in entries:
            yield fuse.Direntry(e)

    def open(self, path, flags):
        """Open a file and return a file descriptor"""
        self.logger.debug(f"open: {path}, flags: {flags}")
        
        item = self._get_drive_item(path)
        if item is None:
            self.logger.error("open: Item is none")
            return -errno.ENOENT
            
        fd = self._get_next_fd()
        self.fd_map[fd] = path
        self.logger.debug("open: Returning file descriptor: " + str(fd))
        return fd

    def read(self, path, size, offset):
        """Read data from a file"""
        self.logger.debug(f"read: {path}, size: {size}, offset: {offset}")
        
        content = None
        with self.cache_lock:
            # Check if we have cached content
            if path in self.content_cache:
                content, timestamp = self.content_cache[path]
                if time.time() - timestamp < self.cache_timeout:
                    return content[offset:offset+size]
        
        try:
            if content is None:
                item = self._get_drive_item(path)
                if item is None:
                    return -errno.ENOENT
                    
                # Download the file from iCloud
                content = item.open(stream=True).raw.read()
                
                with self.cache_lock:
                    self.content_cache[path] = (content, time.time())
                    
            return content[offset:offset+size]
        except Exception as e:
            self.logger.error(f"Error reading file {path}: {str(e)}")
            return -errno.EIO

    def write(self, path, buf, offset):
        """Write data to a file"""
        self.logger.debug(f"write: {path}, offset: {offset}, size: {len(buf)}")
        
        try:
            # Cache the data for later upload on flush/release
            with self.cache_lock:
                if path in self.content_cache:
                    content, _ = self.content_cache[path]
                    if offset == 0:
                        new_content = buf
                    elif offset < len(content):
                        new_content = content[:offset] + buf
                    else:
                        # Handle case where offset > len(content)
                        new_content = content + b'\0' * (offset - len(content)) + buf
                else:
                    if offset > 0:
                        new_content = b'\0' * offset + buf
                    else:
                        new_content = buf
                    
                self.content_cache[path] = (new_content, time.time())
                
            return len(buf)
        except Exception as e:
            self.logger.error(f"Error writing to file {path}: {str(e)}")
            return -errno.EIO

    def flush(self, path):
        """Flush cached data to iCloud (called on close)"""
        self.logger.debug(f"flush: {path}")
        return 0  # We'll actually upload in release()

    def release(self, path, flags):
        """Release a file (close it)"""
        self.logger.debug(f"release: {path}")
        
        try:
            with self.cache_lock:
                if path in self.content_cache:
                    content, timestamp = self.content_cache[path]
                    
                    # Check if we need to upload
                    parent_path = os.path.dirname(path)
                    filename = os.path.basename(path)
                    
                    parent = self._get_drive_item(parent_path)
                    if parent is None:
                        return -errno.ENOENT
                    
                    # TODO: Upload the file to iCloud. BytesIO object has no attribute 'name'
                    # parent.upload(BytesIO(content))
                    
                    # Invalidate caches for this path
                    if path in self.attr_cache:
                        del self.attr_cache[path]
                    
            # Clean up file descriptor
            for fd, p in list(self.fd_map.items()):
                if p == path:
                    del self.fd_map[fd]
                    
            return 0
        except Exception as e:
            self.logger.error(f"Error releasing file {path}: {str(e)}")
            return -errno.EIO

    def mkdir(self, path, mode):
        """Create a directory"""
        self.logger.debug(f"mkdir: {path}, mode: {mode}")
        
        try:
            parent_path = os.path.dirname(path)
            dirname = os.path.basename(path)
            
            parent = self._get_drive_item(parent_path)
            if parent is None:
                return -errno.ENOENT
                
            # Create directory in iCloud
            parent.mkdir(dirname)
            
            # Invalidate parent directory cache
            with self.cache_lock:
                if parent_path in self.dir_cache:
                    del self.dir_cache[parent_path]
                    
            return 0
        except Exception as e:
            raise e
            self.logger.error(f"Error creating directory {path}: {str(type(e))} {str(e)}")
            return -errno.EIO

    def rmdir(self, path):
        """Remove a directory"""
        self.logger.debug(f"rmdir: {path}")
        
        try:
            item = self._get_drive_item(path)
            if item is None:
                return -errno.ENOENT
                
            # Check if directory is empty
            contents = item.dir()
            if len(contents) > 0:
                return -errno.ENOTEMPTY
                
            # Delete the directory
            item.delete()
            
            # Invalidate caches
            parent_path = os.path.dirname(path)
            with self.cache_lock:
                if path in self.dir_cache:
                    del self.dir_cache[path]
                if path in self.attr_cache:
                    del self.attr_cache[path]
                if parent_path in self.dir_cache:
                    del self.dir_cache[parent_path]
                    
            return 0
        except Exception as e:
            self.logger.error(f"Error removing directory {path}: {str(e)}")
            return -errno.EIO

    def unlink(self, path):
        """Remove a file"""
        self.logger.debug(f"unlink: {path}")
        
        try:
            item = self._get_drive_item(path)
            if item is None:
                return -errno.ENOENT
                
            # Delete the file
            item.delete()
            
            # Invalidate caches
            parent_path = os.path.dirname(path)
            with self.cache_lock:
                if path in self.content_cache:
                    del self.content_cache[path]
                if path in self.attr_cache:
                    del self.attr_cache[path]
                if parent_path in self.dir_cache:
                    del self.dir_cache[parent_path]
                    
            return 0
        except Exception as e:
            self.logger.error(f"Error removing file {path}: {str(e)}")
            return -errno.EIO

    def rename(self, oldpath, newpath):
        """Rename a file or directory"""
        self.logger.debug(f"rename: {oldpath} -> {newpath}")
        
        try:
            item = self._get_drive_item(oldpath)
            if item is None:
                return -errno.ENOENT
                
            # Get new parent path and name
            new_parent_path = os.path.dirname(newpath)
            new_name = os.path.basename(newpath)
            
            new_parent = self._get_drive_item(new_parent_path)
            if new_parent is None:
                return -errno.ENOENT
                
            # Currently PyiCloud doesn't have a direct rename method
            # For files, we need to download and re-upload
            if self._get_path_type(oldpath) == 'file':
                content = item.open(stream=True).raw.read()
                new_parent.upload(new_name, BytesIO(content))
                item.delete()
            else:
                # For directories, this is more complex and not fully supported
                return -errno.ENOSYS
                
            # Invalidate caches
            old_parent_path = os.path.dirname(oldpath)
            with self.cache_lock:
                if oldpath in self.content_cache:
                    del self.content_cache[oldpath]
                if oldpath in self.attr_cache:
                    del self.attr_cache[oldpath]
                if oldpath in self.dir_cache:
                    del self.dir_cache[oldpath]
                if old_parent_path in self.dir_cache:
                    del self.dir_cache[old_parent_path]
                if new_parent_path in self.dir_cache:
                    del self.dir_cache[new_parent_path]
                    
            return 0
        except Exception as e:
            self.logger.error(f"Error renaming {oldpath} to {newpath}: {str(e)}")
            return -errno.EIO

    def truncate(self, path, length):
        """Truncate a file to a specified length"""
        self.logger.debug(f"truncate: {path}, length: {length}")
        
        try:
            with self.cache_lock:
                if path in self.content_cache:
                    content, timestamp = self.content_cache[path]
                    if length < len(content):
                        self.content_cache[path] = (content[:length], time.time())
                    else:
                        # Pad with zeros if truncating to larger size
                        self.content_cache[path] = (content + b'\0' * (length - len(content)), time.time())
                else:
                    # If we don't have cached content, we need to download first
                    item = self._get_drive_item(path)
                    if item is None:
                        return -errno.ENOENT
                    content = item.open(stream=True).raw.read()
                    if length < len(content):
                        self.content_cache[path] = (content[:length], time.time())
                    else:
                        # Pad with zeros if truncating to larger size
                        self.content_cache[path] = (content + b'\0' * (length - len(content)), time.time())
                    
            return 0
        except Exception as e:
            self.logger.error(f"Error truncating file {path}: {str(e)}")
            return -errno.EIO

    def mknod(self, path, mode, dev):
        """Create a file node"""
        self.logger.debug(f"mknod: {path}, mode: {mode}")
        
        # Only support regular files
        if not stat.S_ISREG(mode):
            return -errno.ENOSYS
            
        # Initialize with empty content
        with self.cache_lock:
            self.content_cache[path] = (b'', time.time())
            
        return 0

    def utime(self, path, times):
        """Set file times - not supported by iCloud API"""
        self.logger.debug(f"utime: {path}")
        # We just pretend this worked since we can't actually set times in iCloud
        return 0

    def statfs(self):
        """Get filesystem stats"""
        self.logger.debug("statfs")
        
        # Default values since iCloud doesn't provide this info
        block_size = 4096
        blocks = 1000000  # Just a large number
        blocks_free = 800000  # 80% free
        
        return {
            'f_bsize': block_size,
            'f_frsize': block_size,
            'f_blocks': blocks,
            'f_bfree': blocks_free,
            'f_bavail': blocks_free,
            'f_files': 1000000,  # inodes
            'f_ffree': 800000,   # free inodes
            'f_namelen': 255     # max filename length
        }


def parse_config(config_path):
    """Parse the configuration file"""
    try:
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)
        return config
    except Exception as e:
        print(f"Error parsing config file: {str(e)}")
        sys.exit(1)


def main():
    # Set up argument parser
    usage = """
iCloud FUSE: Mount iCloud Drive as a filesystem
    
%prog [options] mountpoint
"""
    
    # Create a new Fuse instance
    fs = ICloudFS(version="%prog " + fuse.__version__,
                  usage=usage,
                  dash_s_do='setsingle')
    
    # Define command line options
    fs.parser.add_option('-c', '--config', dest='config',
                        default=os.path.expanduser('~/.config/icloud-fuse/config.yaml'),
                        help='Path to config file (default: ~/.config/icloud-fuse/config.yaml)')
    fs.parser.add_option('-v', '--debug', dest='debug', action='store_true',
                        help='Enable debug logging')
                        
    # Parse command line
    fs.parse(errex=1)
    args = fs.cmdline[0]
    
    # Configure logging
    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                        level=log_level)
    logger = logging.getLogger('icloud-fuse')
    
    # Parse configuration
    config = parse_config(args.config)
    
    # Get username and password from config
    username = config.get('username')
    password = config.get('password')
    
    if not username or not password:
        logger.error("Username or password not provided in config file")
        sys.exit(1)
        
    # Set up cache directory
    cache_dir = config.get('cache_dir', os.path.expanduser('~/.cache/icloud-fuse'))
    
    # Initialize iCloud connection
    fs.init_icloud(username, password, cache_dir)
    
    # Start FUSE
    fs.main()


if __name__ == '__main__':
    main()
