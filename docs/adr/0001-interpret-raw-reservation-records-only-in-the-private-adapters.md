---
status: accepted
---

# Interpret raw Kea Reservation records only in the private adapters

Kea returns a Reservation as a flat record whose identifier key is also its identifier type, and whose address key differs between DHCPv4 and DHCPv6. Reading those keys at each call site made the identifier type an implicit contract, so consumers ranked identifiers by priority and addressed a Reservation by its IP address, which is not unique across Reservation Scopes. `netbox_kea/reservations.py` now owns the only parser and the only serializer for that shape, `netbox_kea/kea.py` owns the only `reservation-*` commands, and every other module receives typed `Reservation`, `ReservationIdentity` and `ReservationScope` values. The `kea-reservation-command-outside-adapter` OpenGrep rule holds the boundary by refusing a `reservation-*` command anywhere else, because the raw record cannot reach a consumer that never asks Kea for one.
