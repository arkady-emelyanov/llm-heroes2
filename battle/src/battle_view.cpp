/***************************************************************************
 *   Standalone fheroes2 "battle only" build.                              *
 *                                                                         *
 *   See battle_view.h.                                                    *
 ***************************************************************************/

#include "battle_view.h"

namespace
{
    bool forceShowBattle{ false };
    bool pauseBeforeBattleStarts{ false };
}

void BattleView::setForceShow( const bool force )
{
    forceShowBattle = force;
}

bool BattleView::forceShow()
{
    return forceShowBattle;
}

void BattleView::setPauseBeforeStart( const bool pause )
{
    pauseBeforeBattleStarts = pause;
}

bool BattleView::pauseBeforeStart()
{
    return pauseBeforeBattleStarts;
}
