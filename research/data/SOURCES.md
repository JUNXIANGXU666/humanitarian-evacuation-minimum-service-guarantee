# Network sources

Sioux Falls, Anaheim and Winnipeg network and trip files come from the Transportation Networks for Research repository:

https://github.com/bstabler/TransportationNetworks

Source folders: `SiouxFalls`, `Anaheim`, `Winnipeg`. The upstream repository permits academic research use and requests acknowledgement of the data source. The repository and individual network documentation describe the data conventions. Source file SHA-256 hashes are stored with every generated instance.

`raw/ND.json` is the author-specified 13-node, 19-arc diagnostic instance. Its full arc table, demand weights, origins and shelters are reported in the manuscript appendix.

The upstream Winnipeg capacities are uniform arbitrary values, not observed evacuation capacity. The generator uses a common normalised capacity construction documented in the manuscript. All evacuation demands, needs shares and resource budgets are constructed scenario inputs.
