# iCloud Linux FUSE Filesystem

This project provides a FUSE-based driver to mount iCloud Drive as a filesystem on Linux. It allows users to interact with their iCloud Drive files and directories as if they were part of the local filesystem.

## Features

- Mount iCloud Drive as a FUSE filesystem.
- Read and write files directly to iCloud Drive.
- Cache file and directory metadata for improved performance.
- Support for basic filesystem operations such as reading, writing, renaming, and deleting files and directories.

## Requirements

- Python 3
- `fuse-python` library
- `pyicloud` library
- `pyyaml` library
- `jsonpickle` library
- FUSE2 (Filesystem in Userspace)

## Installation

1. **Install Dependencies**:
   - Install FUSE2 and Python dependencies:
     ```bash
     sudo apt install fuse # or `fuse2`
     pip install -r requirements.txt
     ```

2. **Set Up Configuration**:
   - Copy the example configuration file to `/etc/icloud/config.yaml`:
     ```bash
     sudo mkdir -p /etc/icloud
     sudo cp config.example.yaml /etc/icloud/config.yaml
     ```
   - Edit the configuration file to include your iCloud credentials:
     ```yaml
     username: "your_apple_id@example.com"
     password: "your_apple_id_password"
     cache_dir: "/tmp/icloud"
     ```

3. **Install the Driver**:
   - Run the `Makefile` to install the driver and systemd service:
     ```bash
     make install
     ```

4. **Start the Service**:
   - The installation process will prompt you to specify a mount point (default: `~/iCloud`). The service will be enabled and started automatically:
     ```bash
     systemctl enable --now icloud
     ```

5. **Verify the Mount**:
   - Check if the iCloud Drive is mounted at the specified location:
     ```bash
     ls ~/iCloud
     ```

## How It Works

### Overview

The driver uses the `pyicloud` library to interact with iCloud Drive and the `fuse-python` library to implement a FUSE-based filesystem. It provides a seamless interface for accessing iCloud Drive files and directories on Linux.

### Key Components

1. **Driver (`driver.py`)**:
   - Implements the FUSE filesystem operations such as `getattr`, `readdir`, `read`, `write`, `mkdir`, `unlink`, etc.
   - Uses `pyicloud` to interact with iCloud Drive and fetch file metadata and content.
   - Caches file and directory metadata to improve performance and reduce API calls.

2. **Configuration (`config.example.yaml`)**:
   - Stores iCloud credentials and FUSE options.
   - Allows customization of cache directory and mount options.

3. **Systemd Service (`icloud.service`)**:
   - Manages the lifecycle of the FUSE filesystem.
   - Automatically mounts iCloud Drive on system startup.

4. **Makefile**:
   - Automates the installation and uninstallation process.
   - Copies necessary files to appropriate locations and sets up the systemd service.

### Caching Mechanism

The driver caches file and directory metadata to reduce the number of API calls to iCloud. Cached data is stored in memory and expires after 5 minutes. This improves performance when accessing frequently used files and directories.

### Two-Factor Authentication (2FA)

If your iCloud account requires 2FA, the driver will prompt you to enter the verification code during initialization. The code is validated using the `pyicloud` library.

### Supported Operations

- **File Operations**: Read, write, truncate, delete, rename.
- **Directory Operations**: Create, delete, list contents.
- **Metadata Operations**: Get file attributes, set file times (mocked).

## Uninstallation

To uninstall the driver and remove all related files:

1. Stop and disable the systemd service:
   ```bash
   sudo systemctl stop icloud
   sudo systemctl disable icloud
   ```

2. Run the `Makefile` uninstall target:
   ```bash
   make uninstall
   ```

3. Verify that all files have been removed:
   - `/usr/local/bin/icloud`
   - `/etc/icloud/`
   - `/etc/systemd/system/icloud.service`
   - `/tmp/icloud/`

## Troubleshooting

- **Debug Logging**:
  - Enable debug logging by running the driver with the `-f` (foreground), `-v` (verbose), and `-d` (FUSE debug) options:
    ```bash
    ./driver.py -fvd ~/iCloud
    ```

- **Log File**:
  - Check the log file at `/var/log/icloud.log` for detailed error messages.

- **Permissions**:
  - Ensure that the mount point directory has the correct permissions for the user running the service.

## License

This project is licensed under the MIT License. See the LICENSE file for details.
