# Image builders

Each builder downloads official media (checksums and, where the vendor signs them,
signatures are verified), installs the QEMU guest agent so Strata can drive the VM, and
registers the result as a Strata image. Builds run on the isolated `strata-build`
network, which reaches the internet but never the LAN or the host.

Run them as a user in the `libvirt` group (the one `install-host.sh` set up):

```bash
images/linux/build-ubuntu.sh 24.04
images/windows/build-windows11.sh            # downloads the current eval ISO
images/windows/build-windows11.sh --iso /path/to/Windows11.iso   # your own ISO/licence
```

## What the clean images contain

- A local `strata` administrator account. Its password is **not** a secret: Strata resets
  it to a unique value for every VM through the guest agent.
- The QEMU guest agent, running at every boot. Strata relies on it for `exec`, file
  transfer, passwords and screen input — no network needed inside the VM.
- OpenSSH server and (Windows) RDP enabled.
- No automatic disk encryption, so two states can be compared offline.
- Windows Defender is left **on**. To study payloads it would quarantine, disable it in
  that one VM with `images/windows/disable-defender.ps1` — never in the base image.

## Windows notes

- The evaluation edition expires after about 90 days and cannot be activated with a key.
  Rerun the builder to rebuild, or build from your own licensed ISO with `--iso`.
- TPM 2.0 and Secure Boot are provided to the VM, so the install passes Windows 11's
  hardware checks without any bypass.
- Microsoft does not publish a checksum for the eval ISO, so it is size-checked only.
  Pass your own trusted ISO with `--iso` if you prefer.
