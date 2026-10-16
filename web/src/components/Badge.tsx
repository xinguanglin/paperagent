type Tone = 'gray' | 'green' | 'blue' | 'orange' | 'red' | 'purple'

interface BadgeProps {
  readonly text: string
  readonly tone?: Tone
}

export default function Badge({ text, tone = 'gray' }: BadgeProps) {
  return <span className={`badge badge-${tone}`}>{text}</span>
}

export function difficultyTone(difficulty: number): Tone {
  if (difficulty <= 2) return 'green'
  if (difficulty === 3) return 'blue'
  return 'orange'
}

export function difficultyLabel(difficulty: number): string {
  return `通俗度 ${difficulty}/5`
}
