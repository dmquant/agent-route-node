"""rsi_shadow_guard: fail-closed lane enforcement against the pulled rsi block (ARC-299 option i).
Run: python3 -m unittest tests.test_rsi_shadow_guard"""
import copy, types, unittest

from app import rsi_shadow_guard as g

CAPABLE = types.SimpleNamespace(name="x", rsi_shadow_capable=True)
PLAIN = types.SimpleNamespace(name="claude")
ENV = {"PATH": "/usr/bin", "HOME": "/Users/d", "ANTHROPIC_API_KEY": "sk-x", "NODE_KEY": "nk_x", "LANG": "C"}
SERVERS = [{"name": "institute", "headers": {"Authorization": "Bearer t"}}, {"name": "plain", "url": "http://x"}]


def task(**over):
    # the pulled shape (ARC-299 option i): top-level lane + authoritative rsi block; no context/source/priority
    t = {"id": "t1", "lane": "rsi-shadow",
         "rsi": {"lane": "rsi-shadow", "round_id": "r1", "candidate_id": "c1", "readOnly": True,
                 "credentials": "none", "tools": "read-only", "source": "rsi-shadow", "priority": -3}}
    for k, v in over.items():
        if k.startswith("ctx_"):
            t.setdefault("context", {})
            if v is DEL:
                t["context"].pop(k[4:], None)
            else:
                t["context"][k[4:]] = v
        elif k.startswith("rsi_"):
            if v is DEL:
                t["rsi"].pop(k[4:], None)
            else:
                t["rsi"][k[4:]] = v
        elif v is DEL:
            t.pop(k, None)
        else:
            t[k] = v
    return t


DEL = object()


class Guard(unittest.TestCase):
    def test_conforming_task_on_capable_hand_runs_scrubbed(self):
        d = g.guard(task(), CAPABLE, ENV, SERVERS)
        self.assertTrue(d.allowed and d.applies, d.reasons)
        self.assertEqual(d.env, {"PATH": "/usr/bin", "LANG": "C"})
        self.assertEqual(d.mcp_servers, [])
        self.assertEqual(d.tools, ["Read", "Grep", "Glob", "LS"])

    def test_no_lane_passes_through_untouched(self):
        t = {"id": "t", "context": {"a": 1}}
        d = g.guard(t, PLAIN, ENV, SERVERS)
        self.assertTrue(d.allowed and not d.applies)
        self.assertIs(d.env, ENV)
        self.assertIs(d.mcp_servers, SERVERS)

    def test_unsupported_hand_refused(self):
        for h in (PLAIN, types.SimpleNamespace(name="y", rsi_shadow_capable="yes"),
                  types.SimpleNamespace(name="z", rsi_shadow_capable=1)):
            d = g.guard(task(), h, ENV)
            self.assertFalse(d.allowed)
            self.assertIsNone(d.env)

    def test_each_contract_field_is_required_exactly(self):
        bad = [dict(rsi_readOnly=False), dict(rsi_readOnly="true"), dict(rsi_readOnly=1), dict(rsi_readOnly=DEL),
               dict(rsi_credentials="inherit"), dict(rsi_credentials=None), dict(rsi_credentials=DEL),
               dict(rsi_tools="all"), dict(rsi_tools=["Read"]), dict(rsi_tools=DEL),
               dict(rsi_round_id=DEL), dict(rsi_round_id=""), dict(rsi_round_id=7),
               dict(rsi_round_id=" r1"), dict(rsi_candidate_id=DEL), dict(rsi_candidate_id=None),
               dict(rsi_source="agent"), dict(rsi_source=None), dict(rsi_source=DEL),
               dict(rsi_priority=0), dict(rsi_priority=-2), dict(rsi_priority=True),
               dict(rsi_priority="-3"), dict(rsi_priority=None), dict(rsi_priority=DEL),
               dict(source="agent"), dict(priority=0), dict(priority=True)]
        for over in bad:
            d = g.guard(task(**over), CAPABLE, ENV)
            self.assertFalse(d.allowed, over)

    def test_priority_float_minus_three_is_the_same_wire_value(self):
        self.assertTrue(g.guard(task(rsi_priority=-3.0), CAPABLE, ENV).allowed)

    def test_unknown_or_malformed_lane_refused(self):
        for lane in ("RSI-SHADOW", "prod", "", None, 1, ["rsi-shadow"]):
            for over in (dict(rsi_lane=lane), dict(lane=lane), dict(ctx_lane=lane), dict(meta={"lane": lane})):
                d = g.guard(task(**over), CAPABLE, ENV)
                self.assertFalse(d.allowed, (lane, over))
                self.assertTrue(d.applies)

    def test_lane_in_meta_or_top_level_counts_and_disagreement_refuses(self):
        self.assertFalse(g.guard({"meta": {"lane": "rsi-shadow"}}, PLAIN, ENV).allowed)
        self.assertFalse(g.guard({"lane": "rsi-shadow"}, PLAIN, ENV).allowed)
        self.assertFalse(g.guard(task(meta={"lane": "prod"}), CAPABLE, ENV).allowed)
        self.assertTrue(g.guard(task(meta={"lane": "rsi-shadow"}, lane="rsi-shadow"), CAPABLE, ENV).allowed)

    def test_non_object_context_meta_task_refused(self):
        self.assertFalse(g.guard(task(context="rsi-shadow"), CAPABLE, ENV).allowed)
        self.assertFalse(g.guard(task(rsi="rsi-shadow"), CAPABLE, ENV).allowed)
        self.assertFalse(g.guard(task(meta=["lane"]), CAPABLE, ENV).allowed)
        self.assertFalse(g.guard(["x"], CAPABLE, ENV).allowed)
        self.assertFalse(g.guard(None, CAPABLE, ENV).allowed)

    def reasons(self, *a, **k):
        d = g.guard(*a, **k)
        self.assertFalse(d.allowed)
        return " | ".join(d.reasons)

    def test_the_refusal_names_its_actual_cause(self):
        self.assertIn("unknown lane", self.reasons(task(rsi_lane="prod", lane="prod"), CAPABLE, ENV))
        self.assertIn("not an object", self.reasons(task(context="x"), CAPABLE, ENV))
        self.assertIn("rsi block is missing", self.reasons(task(rsi=DEL), CAPABLE, ENV))
        self.assertIn("differs", self.reasons(task(meta={"lane": "prod"}), CAPABLE, ENV))
        self.assertIn("task is not an object", self.reasons(None, CAPABLE, ENV))
        self.assertIn("tools is not a list", self.reasons(task(), CAPABLE, ENV, requested_tools=5))

    def test_rsi_block_must_itself_carry_the_lane(self):
        # lane named only in meta / top level, rsi.lane absent: not the contract payload
        self.assertFalse(g.guard(task(rsi_lane=DEL, meta={"lane": "rsi-shadow"}), CAPABLE, ENV).allowed)
        self.assertFalse(g.guard(task(rsi_lane=DEL), CAPABLE, ENV).allowed)

    def test_pulled_shape_without_context_source_priority_runs(self):
        t = task()
        self.assertNotIn("context", t)
        self.assertNotIn("source", t)
        self.assertNotIn("priority", t)
        self.assertTrue(g.guard(t, CAPABLE, ENV).allowed)

    def test_legacy_context_alone_is_not_enough(self):
        # the old routes/rsi.ts:122 shape with no rsi block: the pull strips it, so it is refused
        t = {"id": "t", "lane": "rsi-shadow", "source": "rsi-shadow", "priority": -3,
             "context": {"lane": "rsi-shadow", "rsi_round_id": "r1", "candidate_id": "c1", "readOnly": True,
                         "credentials": "none", "tools": "read-only"}}
        self.assertFalse(g.guard(t, CAPABLE, ENV).allowed)

    def test_context_present_must_agree_with_rsi_block(self):
        self.assertTrue(g.guard(task(ctx_readOnly=True, ctx_credentials="none", ctx_tools="read-only",
                                     ctx_rsi_round_id="r1", ctx_candidate_id="c1", source="rsi-shadow",
                                     priority=-3), CAPABLE, ENV).allowed)
        for over in (dict(ctx_rsi_round_id="r2"), dict(ctx_candidate_id="c2"), dict(ctx_readOnly=False),
                     dict(ctx_credentials="inherit"), dict(ctx_tools="all")):
            self.assertFalse(g.guard(task(**over), CAPABLE, ENV).allowed, over)

    def test_top_level_null_lane_with_meta_lane_refused(self):
        # agent-route 1c560125: explicit top-level lane None hid meta.lane in the in-tree lane_of
        self.assertFalse(g.guard({"lane": None, "meta": {"lane": "rsi-shadow"}}, PLAIN, ENV).allowed)
        self.assertFalse(g.guard(task(lane=None, meta={"lane": "rsi-shadow"}), CAPABLE, ENV).allowed)
        self.assertFalse(g.guard(task(lane=None), CAPABLE, ENV).allowed)
        self.assertFalse(g.guard(task(lane=DEL, meta={"lane": "prod"}), CAPABLE, ENV).allowed)

    def test_lane_only_in_rsi_block_is_still_lane_work(self):
        d = g.guard(task(rsi_lane="prod", lane=DEL), PLAIN, ENV)
        self.assertFalse(d.allowed)
        self.assertTrue(d.applies)
        d = g.guard({"rsi": {"lane": "rsi-shadow"}}, PLAIN, ENV)
        self.assertFalse(d.allowed)

    def test_rsi_lane_contradicting_top_level_refused(self):
        self.assertFalse(g.guard(task(lane="prod"), CAPABLE, ENV).allowed)
        self.assertFalse(g.guard(task(rsi_lane="prod", lane="rsi-shadow"), CAPABLE, ENV).allowed)

    def test_priority_must_equal_minus_three_not_merely_be_lower(self):
        for p in (-4, -100, -3.5, -2.9):
            self.assertFalse(g.guard(task(rsi_priority=p), CAPABLE, ENV).allowed, p)
            self.assertFalse(g.guard(task(priority=p), CAPABLE, ENV).allowed, p)

    def test_exception_inside_guard_is_a_refusal(self):
        class Boom(object):
            @property
            def rsi_shadow_capable(self):
                raise RuntimeError("x")
        d = g.guard(task(), Boom(), ENV)
        self.assertFalse(d.allowed)
        self.assertIn("RuntimeError", d.refusal_text())

    def test_refusal_text_names_reasons_and_says_not_run(self):
        t = g.guard(task(rsi_readOnly=False), PLAIN, ENV).refusal_text()
        self.assertTrue(t.startswith("[RSI_SHADOW_REFUSED]"))
        self.assertIn("readOnly", t)
        self.assertIn("rsi_shadow_capable", t)
        self.assertIn("not run", t)


class Env(unittest.TestCase):
    def test_only_allowlist_survives_and_input_is_untouched(self):
        e = copy.deepcopy(ENV)
        out = g.scrub_env(e)
        self.assertEqual(set(out), {"PATH", "LANG"})
        self.assertEqual(e, ENV)
        self.assertIsNot(out, e)

    def test_home_and_every_secret_shaped_name_dropped(self):
        e = {k: "v" for k in ("HOME", "OPENAI_API_KEY", "GITHUB_TOKEN", "AGORA_PARTY_KEY", "CLOUDFLARE_API_TOKEN",
                              "AWS_SECRET_ACCESS_KEY", "SSH_AUTH_SOCK", "NODE_AUTH_KEY", "PATH")}
        self.assertEqual(set(g.scrub_env(e)), {"PATH"})

    def test_non_string_values_and_non_dict_env(self):
        self.assertEqual(g.scrub_env({"PATH": 1, "LANG": None}), {})
        self.assertEqual(g.scrub_env(None), {})


class Mcp(unittest.TestCase):
    def test_zero_servers_by_default(self):
        self.assertEqual(g.scrub_mcp(SERVERS), [])
        self.assertEqual(g.scrub_mcp(None), [])

    def test_allowlisted_server_with_a_credential_field_still_dropped(self):
        for field in ("headers", "env", "token", "auth", "Authorization", "api_key", "x_secret", "bearer_token"):
            self.assertEqual(g.scrub_mcp([{"name": "a", field: "v"}], allow=("a",)), [], field)

    def test_allowlisted_credential_free_server_kept_as_a_copy(self):
        src = [{"name": "a", "url": "http://x"}]
        out = g.scrub_mcp(src, allow=("a",))
        self.assertEqual(out, src)
        self.assertIsNot(out[0], src[0])

    def test_malformed_entries_dropped(self):
        self.assertEqual(g.scrub_mcp(["a", None, {"url": "x"}], allow=("a",)), [])


class Tools(unittest.TestCase):
    def test_write_capable_tool_is_refused_not_dropped(self):
        for t in ("Bash", "Write", "Edit", "WebFetch", "read", "mcp__x"):
            d = g.guard(task(), CAPABLE, ENV, requested_tools=["Read", t])
            self.assertFalse(d.allowed, t)

    def test_read_only_subset_passes_and_is_returned(self):
        d = g.guard(task(), CAPABLE, ENV, requested_tools=["Read"])
        self.assertTrue(d.allowed)
        self.assertEqual(d.tools, ["Read"])

    def test_non_list_tools_refused(self):
        self.assertFalse(g.guard(task(), CAPABLE, ENV, requested_tools="Read").allowed)

    def test_read_only_set_has_no_mutating_tool(self):
        self.assertFalse({"Bash", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch"} & set(g.READ_ONLY_TOOLS))


if __name__ == "__main__":
    unittest.main()
