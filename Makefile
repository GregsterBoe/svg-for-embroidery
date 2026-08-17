# Shortcuts. Everything here is one command you could also type by hand —
# `make` is a habit, not a build system, and nothing in the project needs one.

PYTHON ?= python

.PHONY: help test bench bench-save triage prep clean-up convert tracers corpus roundtrip doctor serve degraded

help:
	@echo "make test         run the test suite"
	@echo "make degraded     run it again with every optional extra disabled"
	@echo "make bench        measure the image corpus against the baseline (B1)"
	@echo "make bench-save   record the current numbers as the new baseline"
	@echo "make triage       grade the corpus good/marginal/hopeless (B2)"
	@echo "make prep         measure the corpus after B3 cleans each image"
	@echo "make clean-up     ...and again after B5 cleans up what the tracer drew"
	@echo "make convert      run B6's whole loop over the corpus and grade its gate"
	@echo "make tracers      compare every installed tracer on the corpus (B0)"
	@echo "make corpus       regenerate the corpus images"
	@echo "make roundtrip    prove the writer returns files unchanged (A0 gate)"
	@echo "make doctor       show what this machine can do"
	@echo "make serve        run the local web UI: check, fix, convert (B7)"

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

# B2's gate: does triage put the corpus in the bands a human would? The answer
# is the "triage agrees with 'expect'" line at the foot of the table.
triage:
	$(PYTHON) -m svg_embroidery.cli bench --tracer none --no-compare

# B3's gate: the same corpus, cleaned first. Compare the fit column against
# 'make tracers' — that difference is what preprocessing is worth.
prep:
	$(PYTHON) -m svg_embroidery.cli bench --preprocess --no-compare

# B5's gate: the same corpus, traced and then repaired by its own profile.
# Compare paths and nodes against 'make prep' — the difference is the shapes
# the machine no longer has to sew, and 'passes' says whether the result would
# be accepted by the shop it was aimed at.
clean-up:
	$(PYTHON) -m svg_embroidery.cli bench --preprocess --cleanup --no-compare

# B6's gate: the whole loop — preprocess, trace, clean up, check, and adjust a
# setting and try again — over the corpus, aimed at the profile that has an
# opinion about detail. The line to read is at the foot of the table:
# "converted N/M of the images a human called good or marginal".
convert:
	$(PYTHON) -m svg_embroidery.cli bench -p embroidery-strict --convert --no-compare

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
