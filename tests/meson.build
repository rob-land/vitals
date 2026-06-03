pytest = find_program('pytest', required: false)

if pytest.found()
  test('pytest', pytest,
    args: [meson.current_source_dir()],
    workdir: meson.project_source_root(),
    env: {'PYTHONPATH': meson.project_source_root() / 'src'},
    suite: 'unit',
  )
endif
