project(
  'vitals',
  version: '0.9.0',
  meson_version: '>= 1.0.0',
  license: 'GPL-3.0-or-later',
  default_options: ['warning_level=2'],
)

i18n   = import('i18n')
gnome  = import('gnome')
python = import('python').find_installation('python3')

application_id = 'land.rob.vitals'
prefix     = get_option('prefix')
bindir     = prefix / get_option('bindir')
datadir    = prefix / get_option('datadir')
localedir  = prefix / get_option('localedir')
pkgdatadir = datadir / meson.project_name()
moduledir  = python.get_install_dir() / meson.project_name()

# Bare-string substitutions for XML/desktop/launcher .in files.
conf = configuration_data()
conf.set('PYTHON',     python.full_path())
conf.set('APP_ID',     application_id)
conf.set('VERSION',    meson.project_version())
conf.set('PKGDATADIR', pkgdatadir)
conf.set('LOCALEDIR',  localedir)

# Quoted-string substitutions for Python .in files.
py_conf = configuration_data()
py_conf.set('PYTHON',          python.full_path())
py_conf.set_quoted('APP_ID',     application_id)
py_conf.set_quoted('APP_NAME',   'Vitals')
py_conf.set_quoted('VERSION',    meson.project_version())
py_conf.set_quoted('PKGDATADIR', pkgdatadir)
py_conf.set_quoted('LOCALEDIR',  localedir)

subdir('data')
subdir('src/vitals')
subdir('po')
subdir('tests')

gnome.post_install(
  glib_compile_schemas:    true,
  gtk_update_icon_cache:   true,
  update_desktop_database: true,
)
