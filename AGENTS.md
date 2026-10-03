# Project instructions

## Required: one screen, no page scrolling

The HealthOS interface must fit the available viewport. Preventing vertical and horizontal scrolling of the main page is the highest-priority interface requirement. Every design and behavior change must preserve it.

- Arrange the top navigation, cards, charts, and history panel together according to viewport height.
- Dragging the history panel must not extend the page. It must open in proportion to the drag distance and retain its released height.
- Long content may scroll inside its own panel; the main page must not scroll.
- Using only `overflow: hidden` to clip content or make it unreachable is not a solution.
- Check interface changes on desktop and mobile. Work is incomplete while the main page overflows.
