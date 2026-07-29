#!/usr/bin/env python3
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
spec=spec_from_file_location("check_package", Path(__file__).parents[1]/"scripts/check-package.py")
module=module_from_spec(spec); spec.loader.exec_module(module)
assert "in"+"mobidev" in module.find_prohibited_tokens("author: in"+"mobidev")
assert module.find_prohibited_tokens("author: Myrmex contributors") == []
print("public identity policy test: PASS")
