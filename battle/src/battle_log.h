/***************************************************************************
 *   Standalone fheroes2 "battle only" build.                              *
 *                                                                         *
 *   A record of what actually happened in a battle, written by the engine  *
 *   rather than by either player.                                          *
 *                                                                         *
 *   The harness can only report what it decided; the game's own AI reports *
 *   nothing at all. Neither tells you what the engine then did with those  *
 *   decisions. This log sits at the point where a turn becomes commands,   *
 *   so both sides are recorded the same way and the result can be read     *
 *   back without trusting either player's account of it.                   *
 *                                                                         *
 *   One JSON object per line, so it can be grepped, or loaded a line at a  *
 *   time by anything that wants to analyse a run.                          *
 ***************************************************************************/

#pragma once

#include <string>

namespace Battle
{
    class Arena;
    class Unit;
    class Actions;
}

namespace BattleLog
{
    // Starts a new log at 'path'. An empty path disables logging entirely, which is the default:
    // a battle nobody asked to record should not leave a file behind.
    void open( const std::string & path );

    bool isOpen();

    // Records the armies as they stand before the first turn.
    void recordStart( const Battle::Arena & arena );

    // Records one stack's turn: who acted, what state they were in, and the commands it produced.
    // 'controller' says who decided - the game's own AI, the harness, or a human.
    void recordTurn( const Battle::Arena & arena, const Battle::Unit & unit, const Battle::Actions & actions, const char * controller );

    // Records how the battle ended, and the armies as they finished.
    void recordEnd( const Battle::Arena & arena, const std::string & winner );

    void close();
}
