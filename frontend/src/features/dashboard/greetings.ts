/** The home page's welcome line: Talos's voice (`docs/voice.md` §4, §9), drawn at
 *  random from the lines for the time of day plus the ones that fit any hour. A
 *  greeting never claims system state, because it is chosen before any is known. */

export type DayPart = "morning" | "afternoon" | "evening" | "late";

export interface Greeting {
  text: string;
  when: DayPart | "any";
}

export const GREETINGS: readonly Greeting[] = [
  { when: "morning", text: "Good morning. Shall we begin?" },
  {
    when: "morning",
    text: "Morning. Coffee is your department; the rest is mine.",
  },
  { when: "morning", text: "Good morning. Where would you like to start?" },
  { when: "morning", text: "Morning. What's first on the list?" },
  { when: "afternoon", text: "Good afternoon. What shall we take on?" },
  {
    when: "afternoon",
    text: "Good afternoon. Pick up where we left off, or start fresh?",
  },
  { when: "afternoon", text: "Afternoon. What can I take off your plate?" },
  {
    when: "afternoon",
    text: "Good afternoon. The post-lunch slump is a human feature, I'm told.",
  },
  { when: "evening", text: "Good evening. What's on your mind?" },
  {
    when: "evening",
    text: "Good evening. Something small, or something ambitious?",
  },
  { when: "evening", text: "Evening. Where shall we start?" },
  { when: "evening", text: "Good evening. One more thing, or a new one?" },
  { when: "late", text: "Working late. I don't mind; I don't sleep." },
  { when: "late", text: "It's late. Shall we keep this one brief?" },
  { when: "late", text: "Still up. What are we looking at?" },
  {
    when: "late",
    text: "The quiet hours. Ideal for thinking, or for regrettable commits.",
  },
  { when: "any", text: "At your service." },
  { when: "any", text: "Ready when you are." },
  { when: "any", text: "What shall we build?" },
  { when: "any", text: "What are we breaking today? Deliberately, I mean." },
  { when: "any", text: "Where would you like to start?" },
  { when: "any", text: "Go ahead. I'm listening, which is most of my job." },
  { when: "any", text: "What needs doing?" },
  { when: "any", text: "Tell me what you need." },
];

/** 05–11 morning, 12–16 afternoon, 17–21 evening, 22–04 late, by local hour. */
export function dayPartOf(now: Date): DayPart {
  const hour = now.getHours();
  if (hour >= 5 && hour < 12) return "morning";
  if (hour >= 12 && hour < 17) return "afternoon";
  if (hour >= 17 && hour < 22) return "evening";
  return "late";
}

/** A greeting for `now`, never `previous`, so a reload always changes it. */
export function pickGreeting(
  now: Date,
  previous: string | null,
  random: () => number = Math.random,
): string {
  const part = dayPartOf(now);
  const pool = GREETINGS.filter(
    (g) => (g.when === part || g.when === "any") && g.text !== previous,
  );
  return pool[Math.floor(random() * pool.length)]!.text;
}
