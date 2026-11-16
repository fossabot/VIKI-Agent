import { expect, test } from "vitest";
import type { BotRow } from "../../clients/messaging/botsApi";
import { SEAT_STARTERS } from "../Profiles/seatStarters";
import { botName, seatFace, seatOf, TEAM, teamRoster } from "./team";

function bot(id: string, profileId: string, name: string | null, workspace = "w1"): BotRow {
  return {
    bot_id: id,
    profile_id: profileId,
    workspace_id: workspace,
    profile: name
      ? {
          revision: 1,
          archived: false,
          identity: { name, purpose: "", avatar_asset_id: null, accent: "slate", shape: "rounded" },
        }
      : null,
  } as BotRow;
}

const chiefId = TEAM[0]?.profileId ?? "";

test("TEAM order, ids and roles", () => {
  expect(TEAM.map((s) => s.seat)).toEqual([
    "chief",
    "ops",
    "research",
    "writer",
    "engineer",
    "desk",
    "checker",
  ]);
  expect(TEAM.map((s) => s.role)).toEqual([
    "Front door",
    "Manager",
    "Specialist",
    "Specialist",
    "Specialist",
    "General desk",
    "Quality gate",
  ]);
  expect(TEAM[0]?.profileId).toBe("6a0d02f9-f8dc-4034-941d-03cedde6c839");
  expect(TEAM.find((s) => s.seat === "checker")).toMatchObject({
    name: "Checker",
    starter: { name: "Checker" },
    profileId: "9b0e32bf-2d57-46d8-86ce-799a81ecb248",
  });
  expect(TEAM.find((s) => s.seat === "engineer")).toMatchObject({
    name: "Engineer",
    role: "Specialist",
    starter: { name: "Engineer", accent: "orange" },
    profileId: "3f1c9a6e-5b2d-4e8f-9a07-c41d2b6e8f53",
  });
  expect(TEAM.find((s) => s.seat === "desk")?.starter?.name).toBe("Desk");
});

test("seatOf trims and lowercases, only Bot seats", () => {
  expect(seatOf(" Chief ")).toBe("chief");
  expect(seatOf("checker")).toBe("checker");
  expect(seatOf(" Engineer ")).toBe("engineer");
  expect(seatOf("Planner")).toBeNull();
});

test("seatFace", () => {
  expect(seatFace("checker")).toMatchObject({
    profileId: "9b0e32bf-2d57-46d8-86ce-799a81ecb248",
    identity: { name: "Checker", accent: "red", avatar_asset_id: null, shape: "rounded" },
  });
  expect(seatFace("engineer")).toMatchObject({
    profileId: "3f1c9a6e-5b2d-4e8f-9a07-c41d2b6e8f53",
    identity: { name: "Engineer", accent: "orange" },
  });
  const ops = seatFace("ops");
  expect(ops.profileId).toBe(TEAM[1]?.profileId);
  expect(ops.identity.accent).toBe("slate");
});

test("botName", () => {
  expect(botName(bot("b", "p", "Ann"))).toBe("Ann");
  expect(botName(bot("b", "p", null))).toBe("Unavailable profile");
});

test("id claim beats name claim", () => {
  const named = bot("a", "p1", "Chief");
  const byId = bot("b", chiefId, "Renamed");
  const r = teamRoster([named, byId], "w1");
  expect(r.seats[0]?.bot).toBe(byId);
  expect(r.others).toEqual([named]);
});

test("a padded lowercase name claims, one Bot per seat", () => {
  const a = bot("a", "p1", " chief ");
  const b = bot("b", "p2", "Chief");
  const r = teamRoster([a, b], "w1");
  expect(r.seats[0]?.bot).toBe(a);
  expect(r.seats[1]?.bot).toBeNull();
  expect(r.others).toEqual([b]);
});

test("another vault goes to away", () => {
  const far = bot("a", chiefId, "Chief", "w2");
  const r = teamRoster([far], "w1");
  expect(r.seats[0]?.bot).toBeNull();
  expect(r.away).toEqual([far]);
  expect(r.others).toEqual([]);
});

test("pins every seat's fixed profile id", () => {
  expect(Object.fromEntries(TEAM.map((s) => [s.seat, s.profileId]))).toEqual({
    chief: "6a0d02f9-f8dc-4034-941d-03cedde6c839",
    ops: "e7b63f13-9eb1-4b0d-8df6-cc08d3be33a1",
    research: "b0c7ec1e-85a0-4c20-97f3-e79509370f20",
    writer: "61a1448b-613a-4880-92f7-ec0dd6604bf9",
    engineer: "3f1c9a6e-5b2d-4e8f-9a07-c41d2b6e8f53",
    desk: "b9f8d2d5-0670-4212-a744-f4180c3a36fe",
    checker: "9b0e32bf-2d57-46d8-86ce-799a81ecb248",
  });
  const ops = SEAT_STARTERS.find((s) => s.name === "Ops");
  expect(seatFace("ops").identity.name).toBe(ops?.name);
  expect(seatFace("ops").identity.purpose).toBe(ops?.purpose);
});
