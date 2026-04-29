"""R-p7-deploy-r7: pre-import env setup for torch/numpy/MKL/OpenBLAS.

This module MUST be imported BEFORE any of: numpy, scipy, torch, sklearn,
pandas. It sets OMP_NUM_THREADS=1 and friends so that the BLAS backends
and torch's intra-op pool see "1" at C-extension load time.

Why: torch.set_num_threads() and torch.set_num_interop_threads() only
constrain torch's own thread pools. The underlying OpenBLAS / MKL / OpenMP
backends spawn their OWN worker threads at LIBRARY LOAD time based on
OMP_NUM_THREADS et al. Setting these AFTER the C extension is loaded has
no effect — they're already cached internally.

The bot's CRITICAL contract: bot.py's first executable imports are
    import os  # for sys.path manipulation
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(...), 'scripts', 'cal_mlp'))
    import _thread_env  # this module — sets OMP_NUM_THREADS=1 etc.
    # ... only THEN: import numpy, scipy, torch, etc.

This module ZERO-DEPS by contract — DO NOT import numpy/scipy/etc. here,
because that would defeat the purpose (those libs would load with default
threads before our setdefault fires for the next import). The
`test_thread_env_is_zero_deps_no_numerical_imports` regression locks this.

PRIMARY enforcement: this module runs on every bot.py boot regardless of
launcher (systemd, manual, pytest). The VPS systemd EnvironmentFile
(~/kalshi-bot-repo/.env) is the redundancy / belt — it makes OMP_NUM_THREADS
visible to any subprocess the bot spawns and survives Python crashes.

Verify the contract is intact:
    python3 -m pytest tests/test_cal_mlp_invariants.py -k thread_env -x
"""
import os

for _var in ('OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'OPENBLAS_NUM_THREADS',
             'NUMEXPR_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS'):
    os.environ.setdefault(_var, '1')
