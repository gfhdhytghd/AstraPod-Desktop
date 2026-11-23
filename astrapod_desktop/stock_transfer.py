"""Stock transport for the desktop transfer pages. Call from a device job."""


def import_files(device, paths, progress, *, skip_duplicates, before_commit):
    import hrstock
    from hrstockdevice import stock_mount

    with stock_mount(device, write=True, authorize=True) as root:
        return hrstock.import_files(root, paths, progress, skip_duplicates=skip_duplicates,
                                    before_commit=before_commit)


def preview(device, source, scope):
    import hrstock
    from hrstockdevice import stock_mount

    volume = device.volume
    with stock_mount(device, authorize=True) as root:
        return hrstock.preview(root, source, scope,
                               volume_key=(volume.usb[1], volume.uuid_path.name))


def apply(device, source, plan):
    import hrstock
    from hrstockdevice import stock_mount

    volume = device.volume
    with stock_mount(device, write=True, authorize=True) as root:
        return hrstock.apply(root, source, plan,
                             volume_key=(volume.usb[1], volume.uuid_path.name))
