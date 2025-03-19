install:
	sudo cp ./driver.py /usr/local/bin/icloud
	cp icloud.service /etc/systemd/system/icloud.service
	echo "Where would you like to mount iCloud? (default: /home/user/iCloud)"
	read -p "Mount point: " MOUNT_POINT; \
	if [ -z "$$MOUNT_POINT" ]; then \
		MOUNT_POINT="~/iCloud"; \
	fi; \
	mkdir -p $$MOUNT_POINT
	systemctl enable --now icloud
	echo "iCloud Linux installed successfully. Mounted at $$MOUNT_POINT."

uninstall:
	systemctl stop icloud
	systemctl disable icloud
	rm /etc/systemd/system/icloud.service
	rm /usr/local/bin/icloud
	rm -rf /etc/icloud
	rm -rf /tmp/icloud
	
	@echo "iCloud Linux uninstalled successfully."
