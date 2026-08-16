# Shortcuts. Everything here is one command you could also type by hand —
# `make` is a habit, not a build system, and nothing in the project needs one.

PYTHON ?= python

.PHONY: help test bench bench-save tracers corpus roundtrip doctor serve degraded

help:
	@echo "make test         run the test suite"
	@echo "make degraded     run it again with every optional extra disabled"
	@echo "make bench        measure the image corpus against the baseline (B1)"
	@echo "make bench-save   record the current numbers as the new baseline"
	@echo "make tracers      compare every installed tracer on the corpus (B0)"
	@echo "make corpus       regenerate the corpus images"
	@echo "make roundtrip    prove the writer returns files unchanged (A0 gate)"
	@echo "make doctor       show what this machine can do"
	@echo "make serve        run the local web UI"

test:
	$(PYTHON) -m pytest -q

# The degraded path is a promise the project makes, so it is tested rather than
# assumed: with no renderer, no geometry backend and no Pillow, everything that
# can still run must still run.
degraded:
	SVGEMB_NO_RENDERER=1 SVGEMB_NO_GEOMETRY=1 SVGEMB_NO_RASTER=1 SVGEMB_NO_TRACER=1 \
		$(PYTHON) -m pytest -q

bench:
	$(PYTHON) -m svg_embroidery.cli bench

bench-save:
	$(PYTHON) -m svg_embroidery.cli bench --save

# B0's instrument: the same corpus through every tracer this machine has.
tracers:
	$(PYTHON) -m svg_embroidery.cli bench --tracers

corpus:
	$(PYTHON) bench/make_corpus.py

roundtrip:
	$(PYTHON) -m svg_embroidery.cli roundtrip tests/corpus examples

doctor:
	$(PYTHON) -m svg_embroidery.cli doctor

serve:
	$(PYTHON) -m svg_embroidery.cli serve
