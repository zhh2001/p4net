---
name: Bug report
about: Report a defect in p4net
title: ""
labels: bug
---

## Description

What happened, and what did you expect to happen?

## Reproduction

Minimal steps to reproduce. Include the topology (or a link to a repo / gist) and the exact `p4net` invocation.

## Environment

- p4net version: (output of `python -c "import p4net; print(p4net.__version__)"`)
- Python version: (output of `python --version`)
- OS / kernel: (output of `uname -a`)
- p4c version: (output of `p4c --version`)
- BMv2 version: (output of `simple_switch_grpc --version`)

## Logs

Relevant excerpts from `<log_dir>/<switch>.log` and from `pytest` / `p4net` stderr.
