python.install_sources(
  files(
    '__init__.py',
    '__main__.py',
    'main.py',
    'application.py',
    'background_portal.py',
    'window.py',
    'preferences.py',
    'format.py',
    'logging_setup.py',
  ),
  subdir: meson.project_name(),
)

python.install_sources(
  files(
    'pages/__init__.py',
    'pages/dashboard.py',
    'pages/timeline.py',
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
  ),
  subdir: meson.project_name() / 'ble',
)

python.install_sources(
  files(
    'devices/__init__.py',
    'devices/base.py',
    'devices/manager.py',
    'devices/pinetime.py',
  ),
  subdir: meson.project_name() / 'devices',
)

python.install_sources(
  files(
    'dialogs/__init__.py',
    'dialogs/pairing.py',
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
