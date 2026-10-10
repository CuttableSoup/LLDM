from dm.DM_Types import DMCoreProtocol
import resolution.Combat_Resolution as Combat_Resolution
import resolution.Service_Resolution as Service_Resolution
from resolution.Program_Interpreter import run_program


class ServiceMixin(DMCoreProtocol):
    """!
    @brief Priced services an entity offers (`[[entity.service]]`, docs/services.md): quoted in the
        provider's own prompt at their authored price, bought through the existing "trade" intent,
        and paid with a real currency transfer. DMCore mixin -- only ever composed into DMCore.

        A service is bought where an unmatched "trade" would otherwise fall into ad hoc item
        generation (DM_Improvisation.py's _on_improvisation_requested asks _try_service_purchase
        first), so "buy a night", "hire the mercenary" and "pay for the room" need no new
        classification at all: the keyword gate and the adjudicator's `buy` verdict already send
        them there. What a purchase does is declared on the service, not coded per service: take
        the price, optionally pass time or rest (`blocks`/`overnight`/`rest`), optionally join the
        party (`joins_party`), optionally run an ordinary program (`on_buy`).
    """

    def service_offers_for(self, entity_name):
        """!@brief The "Sells services at fixed prices" fact for entity_name's prompt, as a list of strings."""
        return Service_Resolution.offer_lines(self.entities, entity_name, self.format_currency)

    def _service_providers(self):
        """!@brief Who is present, alive and in view and sells something, in scene order."""
        return [
            name for name in self.scenario_entities
            if name != self.player_name
            # Someone already hired is still a provider, so asking again is refused ("already_joined")
            # instead of falling through to conjuring an item named after the service.
            and (not self._is_party_member(name) or self.entities.get(name, {}).get("hired"))
            and Combat_Resolution.get_current_hp(self.world, name) > 0 and not self.is_hidden(name)
            and Service_Resolution.services_of(self.entities, name)
        ]

    def _try_service_purchase(self, phrase, input_text):
        """!
        @brief Buys the service a trade phrase names from someone present, if it names one.
        @param phrase The player's words for what they are buying.
        @param input_text The whole input.
        @return True if the phrase named a service (bought or refused -- either way narrated), so the
            caller does not also try to conjure an item; False to carry on as before.
        """
        provider, service = Service_Resolution.match_service(
            self.entities, self._service_providers(), phrase, input_text,
        )
        if provider is None:
            return False
        self._buy_service(provider, service, input_text)
        return True

    def _buy_service(self, provider, service, input_text):
        """!
        @brief One purchase: refuse for a stated reason, or take the price and apply what the service
            declares, then publish one "service" item_interaction_resolved.
        @param provider The seller's entity key.
        @param service The matched [[entity.service]] dict.
        @param input_text The player's input, for the narration.
        """
        entity = self.entities.get(provider, {})
        price = Service_Resolution.price_of(service)
        common = {
            "provider": provider, "provider_label": entity.get("name", provider), "service": service["name"],
            "price": price, "content": service.get("content"),
        }
        disposition = self.get_attitude(provider, self.player_name)[0]
        reason = Service_Resolution.refusal(self.entities, self.player_name, provider, service, disposition)
        if reason is None and self.is_hostile(provider, self.player_name):
            reason = "hostile"
        destination = service.get("travel_to")
        takes_time = bool(service.get("rest") or service.get("overnight") or service.get("blocks") or destination)
        if reason is None and takes_time and self._any_hostile_present():
            reason = "enemies_near"
        origin_grid = None
        if reason is None and destination:
            origin_grid, reason = self._carriage_route(destination)
        if reason:
            self._publish_item_interaction("service", service["name"], input_text, False, reason=reason, **common)
            return

        if price:
            self.transfer_currency(self.player_name, provider, price)
        if service.get("attitude_event"):
            self.nudge_attitude_from_event(provider, self.player_name, service["attitude_event"], 1.0)

        extra = {}
        if takes_time and not destination:
            blocks = (
                self.get_time_state()["blocks_per_day"] if service.get("overnight") else int(service.get("blocks", 1) or 1)
            )
            if service.get("rest"):
                result = self.rest(blocks)
                if not result.get("interrupted"):
                    extra.update(healed=result["healed"], blocks_spent=result["blocks_spent"], time=result["time"])
            else:
                extra.update(blocks_spent=blocks, time=self.advance_blocks(blocks))

        joined = False
        if service.get("joins_party"):
            # A hire follows the player from here on (DM_Rules.py's _carry_mounts_into_scene carries
            # `hired` entities into each new scene) and fights beside them like any ally with a
            # [[entity.behavior]] list.
            entity["is_party"] = True
            entity["hired"] = True
            entity["follow_offset"] = service.get("follow_offset", entity.get("follow_offset", 0))
            self._apply_party_formation()
            joined = True

        if service.get("on_buy"):
            run_program(
                service["on_buy"], {"actor": self.player_name, "target": provider},
                self.entities, self.rules, self.event_bus,
            )
        if destination:
            # Last, since arriving changes the scene. The ride is the ordinary grid trip -- terrain,
            # roads, encounters and an ambush pausing it all apply -- at the coach's own pace; a trip
            # an ambush interrupts is resumed and narrated by _resume_pending_downtime afterwards.
            result = self._start_pending_travel(destination, origin_grid, speed=service.get("travel_speed"))
            extra.update(travelled=not result["interrupted"], interrupted=result["interrupted"])
            if not result["interrupted"]:
                extra.update({k: v for k, v in result.items() if k != "interrupted"})
        self._publish_item_interaction("service", service["name"], input_text, True, joined=joined, **common, **extra)

    def _carriage_route(self, destination):
        """!
        @brief Whether a service with `travel_to` can leave from here: where the ride starts from
            and, if it cannot go, why. A landmark inside a town has no grid point of its own, so the
            ride starts from the nearest gridded place up its `return_to` chain (the coach stand in
            Sandpoint leaves from Sandpoint).
        @param destination The service's `travel_to` location key.
        @return (origin_grid, None) or (None, reason) -- "downtime_interrupted" (a trip or rest is
            already paused), "no_route" (the destination or the start has no grid point), or
            "already_there".
        """
        if self.pending_downtime:
            return None, "downtime_interrupted"
        key, seen = self.current_location_key, set()
        while key in self.locations and key not in seen and "grid" not in self.locations[key]:
            seen.add(key)
            key = self.locations[key].get("return_to")
        origin = self.locations.get(key, {}).get("grid")
        if origin is None or "grid" not in self.locations.get(destination, {}):
            return None, "no_route"
        if key == destination:
            return None, "already_there"
        return origin, None
