/***************************************************************************
 *   Standalone fheroes2 "battle only" build.                              *
 *                                                                         *
 *   See battle_log.h.                                                     *
 ***************************************************************************/

#include "battle_log.h"

#include <fstream>
#include <memory>

#include "army_troop.h"
#include "battle_arena.h"
#include "battle_army.h"
#include "battle_command.h"
#include "battle_troop.h"
#include "json.h"
#include "logging.h"
#include "monster.h"

namespace
{
    std::unique_ptr<std::ofstream> logFile;
    uint32_t sequence{ 0 };

    constexpr int boardWidth{ 11 };

    std::string position( const int32_t cell )
    {
        if ( cell < 0 ) {
            return "-";
        }

        return "r" + std::to_string( cell / boardWidth ) + "c" + std::to_string( cell % boardWidth );
    }

    const char * colorName( const PlayerColor color )
    {
        switch ( color ) {
        case PlayerColor::BLUE:
            return "blue";
        case PlayerColor::RED:
            return "red";
        default:
            return "none";
        }
    }

    void writeStack( Json::Writer & writer, const Battle::Unit & unit )
    {
        writer.startObject();
        writer.value( "uid", static_cast<int64_t>( unit.GetUID() ) );
        writer.value( "name", unit.GetName() );
        writer.value( "color", colorName( unit.GetArmyColor() ) );
        writer.value( "count", static_cast<int64_t>( unit.GetCount() ) );
        writer.value( "cell", position( unit.GetHeadIndex() ) );
        writer.value( "hit_points_left", static_cast<int64_t>( unit.GetHitPointsLeft() ) );
        writer.endObject();
    }

    void writeArmy( Json::Writer & writer, const char * key, Battle::Force & force )
    {
        writer.startArray( key );

        for ( const Battle::Unit * unit : force ) {
            if ( unit != nullptr && unit->isValid() ) {
                writeStack( writer, *unit );
            }
        }

        writer.endArray();
    }

    const char * commandName( const Battle::CommandType type )
    {
        switch ( type ) {
        case Battle::CommandType::MOVE:
            return "move";
        case Battle::CommandType::ATTACK:
            return "attack";
        case Battle::CommandType::SPELLCAST:
            return "cast";
        case Battle::CommandType::MORALE:
            return "morale";
        case Battle::CommandType::CATAPULT:
            return "catapult";
        case Battle::CommandType::TOWER:
            return "tower";
        case Battle::CommandType::RETREAT:
            return "retreat";
        case Battle::CommandType::SURRENDER:
            return "surrender";
        case Battle::CommandType::SKIP:
            return "skip";
        case Battle::CommandType::TOGGLE_AUTO_COMBAT:
            return "toggle_auto_combat";
        case Battle::CommandType::QUICK_COMBAT:
            return "quick_combat";
        default:
            return "unknown";
        }
    }

    // Decodes a command's parameters into named fields. Reading them consumes the command, so this
    // works on a copy - the original still has to be applied to the arena afterwards.
    void writeCommand( Json::Writer & writer, const Battle::Command & command )
    {
        Battle::Command copy = command;

        writer.startObject();
        writer.value( "type", commandName( copy.GetType() ) );

        switch ( copy.GetType() ) {
        case Battle::CommandType::MOVE: {
            writer.value( "uid", static_cast<int64_t>( copy.GetNextValue() ) );
            writer.value( "to", position( copy.GetNextValue() ) );
            break;
        }
        case Battle::CommandType::ATTACK: {
            writer.value( "uid", static_cast<int64_t>( copy.GetNextValue() ) );
            writer.value( "target_uid", static_cast<int64_t>( copy.GetNextValue() ) );
            writer.value( "move_to", position( copy.GetNextValue() ) );
            writer.value( "strike_cell", position( copy.GetNextValue() ) );
            writer.value( "direction", static_cast<int64_t>( copy.GetNextValue() ) );
            break;
        }
        case Battle::CommandType::SPELLCAST: {
            const int spellId = copy.GetNextValue();
            writer.value( "spell_id", static_cast<int64_t>( spellId ) );
            writer.value( "spell", Spell( spellId ).GetName() );
            writer.value( "at", position( copy.GetNextValue() ) );
            break;
        }
        case Battle::CommandType::SKIP:
        case Battle::CommandType::MORALE: {
            writer.value( "uid", static_cast<int64_t>( copy.GetNextValue() ) );
            break;
        }
        default:
            break;
        }

        writer.endObject();
    }

    void writeLine( const Json::Writer & writer )
    {
        if ( logFile == nullptr ) {
            return;
        }

        *logFile << writer.str() << '\n';
        logFile->flush();
    }
}

void BattleLog::open( const std::string & path )
{
    close();

    if ( path.empty() ) {
        return;
    }

    auto file = std::make_unique<std::ofstream>( path, std::ios::out | std::ios::trunc );

    if ( !file->is_open() ) {
        // A log that cannot be written is worth saying out loud, but not worth stopping a battle for.
        ERROR_LOG( "Cannot write the battle log to '" << path << "'" )
        return;
    }

    logFile = std::move( file );
    sequence = 0;
}

bool BattleLog::isOpen()
{
    return logFile != nullptr;
}

void BattleLog::recordStart( const Battle::Arena & arena )
{
    if ( !isOpen() ) {
        return;
    }

    Json::Writer writer;

    writer.startObject();
    writer.value( "event", "battle_start" );
    writer.value( "seq", static_cast<int64_t>( ++sequence ) );

    writeArmy( writer, "attacker", arena.getAttackingForce() );
    writeArmy( writer, "defender", arena.getDefendingForce() );

    writer.endObject();

    writeLine( writer );
}

void BattleLog::recordTurn( const Battle::Arena & arena, const Battle::Unit & unit, const Battle::Actions & actions, const char * controller )
{
    if ( !isOpen() ) {
        return;
    }

    Json::Writer writer;

    writer.startObject();
    writer.value( "event", "turn" );
    writer.value( "seq", static_cast<int64_t>( ++sequence ) );
    writer.value( "round", static_cast<int64_t>( arena.GetTurnNumber() ) );
    writer.value( "side", unit.GetArmyColor() == arena.getAttackingArmyColor() ? "attacker" : "defender" );
    writer.value( "controller", controller );

    // The stack as it was when it decided, so the log shows the state a choice was made in rather
    // than the state it left behind.
    writer.startObject( "acting" );
    writer.value( "uid", static_cast<int64_t>( unit.GetUID() ) );
    writer.value( "name", unit.GetName() );
    writer.value( "count", static_cast<int64_t>( unit.GetCount() ) );
    writer.value( "cell", position( unit.GetHeadIndex() ) );
    writer.endObject();

    writer.startArray( "commands" );
    for ( const Battle::Command & command : actions ) {
        writeCommand( writer, command );
    }
    writer.endArray();

    // The board as it stands before these commands are applied. Two consecutive turn records
    // therefore bracket one turn's effect: whatever differs between them is what the commands did.
    writer.startObject( "board" );
    writeArmy( writer, "attacker", arena.getAttackingForce() );
    writeArmy( writer, "defender", arena.getDefendingForce() );
    writer.endObject();

    writer.endObject();

    writeLine( writer );
}

void BattleLog::recordEnd( const Battle::Arena & arena, const std::string & winner )
{
    if ( !isOpen() ) {
        return;
    }

    Json::Writer writer;

    writer.startObject();
    writer.value( "event", "battle_end" );
    writer.value( "seq", static_cast<int64_t>( ++sequence ) );
    writer.value( "rounds", static_cast<int64_t>( arena.GetTurnNumber() ) );
    writer.value( "winner", winner );

    writeArmy( writer, "attacker", arena.getAttackingForce() );
    writeArmy( writer, "defender", arena.getDefendingForce() );

    writer.endObject();

    writeLine( writer );
}

void BattleLog::close()
{
    logFile.reset();
}
