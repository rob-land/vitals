blueprint_compiler = find_program('blueprint-compiler')

blueprint_sources = files(
  'window.blp',
  'help-overlay.blp',
)

blueprints = custom_target('blueprints',
  input:  blueprint_sources,
  output: '.',
  command: [blueprint_compiler, 'batch-compile', '@OUTPUT@', '@CURRENT_SOURCE_DIR@', '@INPUT@'],
)

gresource = gnome.compile_resources(
  application_id,
  application_id + '.gresource.xml',
  gresource_bundle: true,
  install: true,
  install_dir: pkgdatadir,
  dependencies: blueprints,
  source_dir: meson.current_build_dir(),
)
