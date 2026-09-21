/***************************************************************************
 *   Standalone fheroes2 "battle only" build.                              *
 *                                                                         *
 *   Whether a battle is drawn at all.                                      *
 *                                                                         *
 *   The engine decides this from whether a human is playing: a battle with *
 *   no human participant is resolved without building a Battle::Interface, *
 *   because normally nobody is there to watch it.                          *
 *                                                                         *
 *   That reasoning breaks here. A side played by the harness is marked as  *
 *   AI-controlled so the engine routes it to the planner, where the        *
 *   external AI hook intercepts it - so a model-versus-AI battle has no    *
 *   human participant by the engine's definition, and would be fought      *
 *   invisibly. Watching the model play is the entire point, so this flag   *
 *   lets the decision be made explicitly.                                  *
 ***************************************************************************/

#pragma once

namespace BattleView
{
    // When true, the battle is drawn even if no human is playing it.
    void setForceShow( const bool force );

    bool forceShow();

    // When true, the battle waits for the user to confirm before the first turn is taken. The
    // battlefield is already drawn by then, so it doubles as a pause screen - useful when the
    // battle is being recorded and the recorder has to be started first.
    void setPauseBeforeStart( const bool pause );

    bool pauseBeforeStart();
}
