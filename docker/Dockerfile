# Runtime for the designgears patched Wine build (needs glibc >= 2.38).
# Lets Ubuntu/Pop!_OS 22.04 (glibc 2.35) run it without upgrading the host.
FROM ubuntu:24.04
ARG USERNAME=fusion
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      libx11-6 libxext6 libxrender1 libxrandr2 libxi6 libxcursor1 libxinerama1 libxcomposite1 \
      libxdamage1 libxfixes3 libxxf86vm1 libxkbcommon0 libxkbcommon-x11-0 libxcb1 \
      libfreetype6 libfontconfig1 fonts-dejavu-core libgnutls30t64 libgl1 libegl1 libglx0 \
      libvulkan1 mesa-vulkan-drivers libdbus-1-3 libpulse0 libasound2t64 libgstreamer1.0-0 \
      libgstreamer-plugins-base1.0-0 libcups2t64 libkrb5-3 libgssapi-krb5-2 libpcap0.8t64 \
      libusb-1.0-0 libudev1 libsdl2-2.0-0 libv4l-0t64 libunwind8 libosmesa6 \
      winbind ca-certificates xdg-utils procps locales tzdata \
    && rm -rf /var/lib/apt/lists/*
# NVIDIA Vulkan ICD (the nvidia container runtime mounts the library, not the json)
RUN mkdir -p /usr/share/vulkan/icd.d && printf '{"file_format_version":"1.0.0","ICD":{"library_path":"libGLX_nvidia.so.0","api_version":"1.3"}}\n' > /usr/share/vulkan/icd.d/nvidia_icd.json
# Locale: without it Wine starts in "C" and Fusion crashes in nsaddinmgr10 ("initialize server addins").
# Use your own locale here.
ARG LOCALE=en_US.UTF-8
RUN sed -i "s/^# *\(${LOCALE}\)/\1/" /etc/locale.gen && locale-gen
ENV LANG=${LOCALE} LC_ALL=${LOCALE}
# Time zone INSIDE the image: bind-mounting /etc/localtime does not work (it is a symlink in the image)
# and Fusion then shows "your time is not synchronized with the server". Use your own zone.
ARG TZONE=Etc/UTC
RUN ln -sf /usr/share/zoneinfo/${TZONE} /etc/localtime && echo ${TZONE} > /etc/timezone
# uid 1000 is "ubuntu" in 24.04. Wine names the profile folder after the user (drive_c/users/<name>),
# which must match the host user that created the prefix, or you lose your login.
RUN usermod -l ${USERNAME} -d /home/${USERNAME} ubuntu && groupmod -n ${USERNAME} ubuntu
