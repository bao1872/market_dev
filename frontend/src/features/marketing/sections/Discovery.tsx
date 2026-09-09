import clsx from 'clsx'
import ScrollReveal from '../components/ScrollReveal'
import SectionHeading from '../components/SectionHeading'
import { DISCOVERY } from '../data/copy'
import styles from '../marketing.module.scss'

export default function Discovery() {
  return (
    <section className={styles.section} id="discovery" data-testid="marketing-discovery">
      <div className={styles.container}>
        <SectionHeading
          index={DISCOVERY.index}
          eyebrow={DISCOVERY.eyebrow}
          title={DISCOVERY.title}
          subtitle={DISCOVERY.subtitle}
        />
        <div className={clsx(styles.grid, styles.grid2)}>
          {DISCOVERY.entries.map((entry, i) => (
            <ScrollReveal key={entry.key} className={styles.revealFill} delay={i * 90}>
              <article className={styles.card}>
                <h3 className={styles.cardTitle}>{entry.title}</h3>
                <p className={styles.cardDesc}>{entry.desc}</p>
                <ul className={styles.bullets}>
                  {entry.bullets.map((b) => (
                    <li key={b} className={styles.bullet}>
                      {b}
                    </li>
                  ))}
                </ul>
              </article>
            </ScrollReveal>
          ))}
        </div>
      </div>
    </section>
  )
}
