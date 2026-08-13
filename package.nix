{
  lib,
  self,
  rich,
  openai,
  pyyaml,
  jsonschema,
  poetry-core,
  buildPythonApplication,
}:

buildPythonApplication rec {
  pname = "cka_mock";
  version = "unstable-${self.shortRev or "dirty"}";

  pyproject = true;

  src = self;
  setSourceRoot = ''
    sourceRoot="$(echo */src)"
  '';

  build-system = [
    poetry-core
  ];

  dependencies = [
    rich
    openai
    pyyaml
    jsonschema
  ];

  dontCheckRuntimeDeps = true;

  pythonImportsCheck = [
    "cka_mock"
  ];

  meta = {
    mainProgram = "cka-mock";
  };
}
