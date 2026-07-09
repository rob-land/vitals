python.install_sources(
  files(
    '__init__.py',
    '__main__.py',
    'main.py',
    'alarms.py',
    'notifications.py',
    'application.py',
    'background_portal.py',
    'window.py',
    'preferences.py',
    'format.py',
    'trends.py',
    'logging_setup.py',
  ),
  subdir: meson.project_name(),
)

python.install_sources(
  files(
    'pages/__init__.py',
    'pages/dashboard.py',
    'pages/timeline.py',
    'pages/metric_detail.py',
    'pages/devices.py',
    'pages/device_detail.py',
  ),
  subdir: meson.project_name() / 'pages',
)

python.install_sources(
  files(
    'ble/__init__.py',
    'ble/bluetooth_state.py',
    'ble/manager.py',
    'ble/scan_broker.py',
  ),
  subdir: meson.project_name() / 'ble',
)

python.install_sources(
  files(
    'devices/__init__.py',
    'devices/and_oximeter.py',
    'devices/base.py',
    'devices/bangle.py',
    'devices/bangle_dfu.py',
    'devices/bangle_store.py',
    'devices/keeper.py',
    'devices/manager.py',
    'devices/omron_bp.py',
    'devices/pinetime.py',
    'devices/store.py',
    'devices/waterh_bottle.py',
    'devices/weather.py',
    'devices/yucheng_ring.py',
  ),
  subdir: meson.project_name() / 'devices',
)

python.install_sources(
  files(
    'devices/sensors/__init__.py',
    'devices/sensors/decoders.py',
    'devices/sensors/gatt.py',
    'devices/sensors/plugin.py',
  ),
  subdir: meson.project_name() / 'devices' / 'sensors',
)

python.install_sources(
  files(
    'devices/pebble/__init__.py',
    'devices/pebble/fw_update.py',
    'devices/pebble/pbw.py',
    'devices/pebble/pebble.py',
    'devices/pebble/pebble_appinstall.py',
    'devices/pebble/pebble_health.py',
    'devices/pebble/pebble_store.py',
    'devices/pebble/pebble_weather.py',
    'devices/pebble/timeline.py',
    'devices/pebble/ppogatt.py',
    'devices/pebble/protobuf_log.py',
  ),
  subdir: meson.project_name() / 'devices' / 'pebble',
)

python.install_sources(
  files(
    'dialogs/__init__.py',
    'dialogs/alarms_dialog.py',
    'dialogs/app_store_dialog.py',
    'dialogs/bangle_firmware_dialog.py',
    'dialogs/firmware_dialog.py',
    'dialogs/pairing.py',
    'dialogs/pbw_install_dialog.py',
  ),
  subdir: meson.project_name() / 'dialogs',
)

python.install_sources(
  files(
    'widgets/__init__.py',
    'widgets/charts.py',
  ),
  subdir: meson.project_name() / 'widgets',
)

python.install_sources(
  files(
    'core/__init__.py',
    'core/catalog.py',
    'core/csv_export.py',
    'core/errors.py',
    'core/events.py',
    'core/migrate.py',
    'core/records.py',
    'core/replicate.py',
    'core/resources.py',
    'core/store.py',
    'core/units.py',
  ),
  subdir: meson.project_name() / 'core',
)

python.install_sources(
  files(
    'ingest/__init__.py',
    'ingest/builders.py',
    'ingest/recorder.py',
  ),
  subdir: meson.project_name() / 'ingest',
)

python.install_sources(
  files(
    'sources/__init__.py',
    'sources/food.py',
    'sources/food_cache.py',
    'sources/food_lookup.py',
    'sources/measurements.py',
    'sources/usda.py',
    'sources/water.py',
  ),
  subdir: meson.project_name() / 'sources',
)

configure_file(
  input: 'const.py.in',
  output: 'const.py',
  configuration: py_conf,
  install: true,
  install_dir: moduledir,
)

configure_file(
  input: 'vitals.in',
  output: 'vitals',
  configuration: conf,
  install: true,
  install_dir: bindir,
  install_mode: 'rwxr-xr-x',
)
