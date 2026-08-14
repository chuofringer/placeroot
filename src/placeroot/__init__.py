"""PlaceRoot — ground AI agents in Overture Maps open data.

This file exists to make ``placeroot`` a regular package rather than an
implicit namespace package. Namespace packages are assembled from every
``placeroot`` directory found on ``sys.path``, so a stale checkout or a
dead editable install elsewhere on the path could shadow or split the
installed package — seen in the wild as ``No module named
'placeroot.data'`` from an environment that also carried a leftover
editable install. A regular package resolves to exactly one directory.
"""
