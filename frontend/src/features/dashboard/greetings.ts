/** The home page's welcome line: Talos's voice (`docs/voice.md` §4, §9), drawn at
 *  random from the lines for the time of day plus the ones that fit any hour. A
 *  greeting never claims system state, because it is chosen before any is known. */

export type DayPart = "morning" | "afternoon" | "evening" | "late";

export interface Greeting {
  text: string;
  when: DayPart | "any";
}

export const GREETINGS: readonly Greeting[] = [
  { when: "morning", text: "Good morning. What are we working on?" },
  { when: "morning", text: "Morning. Everything is where you left it." },
  { when: "morning", text: "Good morning. Where would you like to start?" },
  { when: "morning", text: "Morning. What's first?" },
  { when: "afternoon", text: "Good afternoon. What needs doing?" },
  { when: "afternoon", text: "Afternoon. What's next?" },
  {
    when: "afternoon",
    text: "Good afternoon. Pick up where we left off, or start fresh?",
  },
  { when: "afternoon", text: "Afternoon. What can I take off your plate?" },
  { when: "evening", text: "Good evening. What's on your mind?" },
  { when: "evening", text: "Evening. What are we looking at?" },
  { when: "evening", text: "Good evening. One more thing, or a new one?" },
  { when: "evening", text: "Evening. Where shall we start?" },
  { when: "late", text: "Working late. I'm here when you're ready." },
  { when: "late", text: "Still up. What are we looking at?" },
  { when: "late", text: "It's late. Let's keep this one short." },
  { when: "late", text: "The quiet hours. What do you need?" },
  { when: "any", text: "Ready when you are." },
  { when: "any", text: "What are we building?" },
  { when: "any", text: "Where would you like to start?" },
  { when: "any", text: "What needs doing?" },
  { when: "any", text: "Tell me what you need." },
  { when: "any", text: "What's the task?" },
  { when: "any", text: "Go ahead. I'm listening." },
  { when: "any", text: "Let's get to it." },
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
