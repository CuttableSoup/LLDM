# Deepen Travel and Time by extracting read-only lookups, not by converting mixins to hosts

The collaborator-plus-host shape used by `DM_Combat` and `DM_Enforcement` leaves a delegating shell (hook methods, property shims) behind the mixin. `DM_Travel.py` and `DM_Time.py` write almost no state; they are mostly world-map and calendar lookups over rules data, so those move into pure modules and the mixins keep only the steps that change state. Whole-mixin host conversion is for mutation-heavy mixins such as `DM_Improvisation.py`, and is decided separately.
