python.install_sources(
  files(
    '__init__.py',
    '__main__.py',
    'main.py',
    'application.py',
    'window.py',
    'pulse_client.py',
    'format.py',
    'logging_setup.py',
  ),
  subdir: meson.project_name(),
)

python.install_sources(
  files(
    'pages/__init__.py',
    'pages/today.py',
    'pages/trends.py',
    'pages/permissions.py',
  ),
  subdir: meson.project_name() / 'pages',
)

python.install_sources(
  files(
    'widgets/__init__.py',
    'widgets/charts.py',
  ),
  subdir: meson.project_name() / 'widgets',
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
