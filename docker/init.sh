#!/bin/sh
# /sbin/init -- Firecracker microVM init script (PID 1).
# Mounts pseudo-filesystems, sets up overlay root, starts fc-agent,
# and waits indefinitely for host commands via vsock.

set -e

# Mount pseudo-filesystems
mount -t proc proc /proc
mount -t sysfs sys /sys
mount -t devtmpfs dev /dev
mount -t tmpfs tmpfs /tmp

# Set up writable overlay on root
# Lower: read-only rootfs (/dev/vda, already mounted at / by kernel with rw)
# Upper: tmpfs-backed writable layer
mkdir -p /overlay/upper /overlay/work
mount -t tmpfs -o size=256M overlay-tmpfs /overlay
mkdir -p /overlay/upper /overlay/work

# Remount root as overlay with writable upper
mount -t overlay overlay \
    -o lowerdir=/,upperdir=/overlay/upper,workdir=/overlay/work \
    /mnt

# Pivot into the overlay root
# (keep it simple: just bind-mount critical paths from overlay)
# For a full pivot_root we'd need initramfs; this overlay-on-root approach
# is sufficient for worker workloads
mount --bind /mnt/etc /etc
mount --bind /mnt/usr /usr
mount --bind /mnt/home /home
mount --bind /mnt/sbin /sbin
mount --bind /mnt/bin /bin
mount --bind /mnt/lib /lib

# Mount worktree drive if present (/dev/vdb)
if [ -e /dev/vdb ]; then
    mkdir -p /work
    mount -t ext4 /dev/vdb /work
fi

# Set hostname
hostname "studio-vm-$(cat /sys/class/vsock/vsock/guest_cid 2>/dev/null || echo '?')"

# Start guest agent on vsock port 52
echo "[init] Starting studio-fc-agent..."
/sbin/studio-fc-agent &

# Wait for agent to exit or for reset signal
# When agent exits (reset requested), clean up and reboot
wait $!
echo "[init] Agent exited, rebooting..."
reboot -f
